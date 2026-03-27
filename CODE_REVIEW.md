# LeStash Code Review

**Date**: 2026-03-25
**Scope**: Full codebase review - core, server, plugins, frontend, deployment, configuration
**Version Reviewed**: 1.18.3

---

## Executive Summary

LeStash is a well-structured UV workspace monorepo with solid foundations: parameterized SQL queries, comprehensive type annotations, good test coverage (41+ tests), and strong commit hygiene following Angular conventions. However, several issues warrant attention across security, reliability, and code quality dimensions.

**Overall Assessment**: Production-ready for personal use with caveats. Address critical security and reliability items before broader deployment.

| Area | Issues | Severity |
|------|--------|----------|
| Core Package | 13 | 5 High, 5 Medium, 3 Low |
| Server (FastAPI) | 16 | 6 High, 7 Medium, 3 Low |
| Source Plugins | 16 | 7 High, 6 Medium, 3 Low |
| Frontend (Tauri) | 10 | 3 High, 5 Medium, 2 Low |
| Deployment | 7 | 1 High, 4 Medium, 2 Low |
| Configuration | 8 | 1 High, 2 Medium, 5 Low |

---

## 1. Core Package (`packages/lestash/`)

### Critical Issues

#### 1.1 Unhandled JSON Deserialization (HIGH)

**Files**: `packages/lestash/src/lestash/models/item.py:39-46`, `packages/lestash/src/lestash/models/source.py:18-26`

`json.loads()` is called without try-except. Corrupted database metadata will crash the application.

```python
if data.get("metadata"):
    data["metadata"] = json.loads(data["metadata"])  # No error handling
```

**Risk**: JSONDecodeError crashes any command that loads items.
**Recommendation**: Wrap in try-except, log warning, and provide empty dict fallback.

#### 1.2 Missing Transaction Rollback on Sync Error (HIGH)

**File**: `packages/lestash/src/lestash/cli/sources.py:100-165`

Exception in the sync loop doesn't rollback uncommitted inserts. If an exception occurs mid-loop, partially inserted items persist in the database.

```python
try:
    for item in plugin.sync(plugin_config):
        conn.execute(...)  # Items inserted in loop
    conn.commit()
except Exception as e:
    status = "failed"
    # NO ROLLBACK - partial changes persist
```

**Recommendation**: Add `conn.rollback()` in the except block or wrap sync work in an explicit transaction.

#### 1.3 Unreliable rowcount for Insert vs Update Detection (HIGH)

**File**: `packages/lestash/src/lestash/cli/sources.py:100-145`

With `ON CONFLICT ... DO UPDATE`, `rowcount` returns 1 for both INSERT and UPDATE operations. `items_updated` is never incremented (always 0).

**Recommendation**: Use `RETURNING` clause or `changes()` to distinguish inserts from updates.

#### 1.4 Database Connection Context Manager Missing Rollback (HIGH)

**File**: `packages/lestash/src/lestash/core/database.py:282-285`

```python
try:
    yield conn
finally:
    conn.close()  # No rollback on exception
```

**Recommendation**: Add exception handling with rollback before close.

#### 1.5 PRAGMA Statement String Interpolation (MEDIUM)

**File**: `packages/lestash/src/lestash/core/database.py:224`

```python
conn.execute(f"PRAGMA user_version = {version}")
```

While PRAGMA doesn't accept parameters, this violates parameterization discipline. Add integer validation/assertion.

### Medium Issues

#### 1.6 Version Number Mismatch

- `packages/lestash/src/lestash/__init__.py` has `0.1.0`
- `packages/lestash/pyproject.toml` has `1.18.3`

Use a single source of truth for version numbers.

#### 1.7 Overly Broad Exception Handling in Plugin Loading

**File**: `packages/lestash/cli/main.py:33-34`

```python
except Exception as e:
    logger.warning(f"Failed to register commands for '{name}': {e}")
```

Catches all exceptions including SystemExit. Use more specific exception types.

#### 1.8 Silent Logging Failures

**File**: `packages/lestash/core/logging.py:102-125`

`DatabaseHandler.emit()` swallows exceptions via `self.handleError(record)`. Log to stderr when database logging fails.

#### 1.9 Unused Custom Exceptions

**File**: `packages/lestash/src/lestash/core/exceptions.py`

Defines `ConfigError`, `DatabaseError`, `PluginError`, `SyncError` but none are used. Either adopt them or remove them.

#### 1.10 Lazy Imports Inside Methods

**Files**: `models/item.py:41`, `models/source.py:21`

`import json` inside methods instead of at module level. Move to top of file.

### Positive Findings

- All SQL queries use parameterized queries (except PRAGMA noted above)
- Comprehensive type annotations throughout
- mypy passes cleanly
- WAL mode enabled for concurrent access
- Foreign key constraints enforced
- Clean migration system with versioned schema
- Rich CLI output with good UX

---

## 2. Server (`packages/lestash-server/`)

### Security Concerns

#### 2.1 Overly Permissive CORS Configuration (HIGH)

**File**: `packages/lestash-server/src/lestash_server/app.py:34-45`

```python
allow_methods=["*"]
allow_headers=["*"]
```

Combined with regex `r"https://.*\.ts\.net(:\d+)?"` which allows any Tailscale subdomain.

**Recommendation**: Whitelist specific methods (GET, POST, OPTIONS) and specific headers.

#### 2.2 File Upload Reads Entire File Before Size Check (HIGH)

**File**: `packages/lestash-server/src/lestash_server/routes/imports.py:23`

```python
data = await file.read()  # Full file loaded into memory
# THEN checks size limit (50MB)
```

**Recommendation**: Validate content-length header first or use streaming with size limit.

#### 2.3 Dangerous CSP Configuration in Tauri (HIGH)

**File**: `app/src-tauri/tauri.conf.json:19`

- `dangerousDisableAssetCspModification: true` disables CSP protection entirely
- `'unsafe-inline'` allows arbitrary inline scripts
- `connect-src` allows connections to ANY http/https URL

**Recommendation**: Enable CSP with specific allowed origins and script hashes/nonces.

#### 2.4 ZIP Parsing Missing Error Handling (MEDIUM)

**File**: `packages/lestash-server/src/lestash_server/routes/imports.py:107-140`

`_parse_zip()` doesn't catch all malformed ZIP exceptions. Could cause server crash on corrupted files.

#### 2.5 LLM Response Structure Not Validated (MEDIUM)

**File**: `packages/lestash-server/src/lestash_server/routes/voice.py:52`

```python
data["choices"][0]["message"]["content"]  # Assumes structure without validation
```

### Code Quality

#### 2.6 DRY Violation - Duplicate SQL (MEDIUM)

Item insert SQL is duplicated across three files:
- `routes/imports.py:67-89`
- `routes/sources.py:86-110`
- `routes/items.py:139-161`

**Recommendation**: Extract to a shared function in the database module.

#### 2.7 Approximate Pagination Filtering (MEDIUM)

**File**: `packages/lestash-server/src/lestash_server/routes/items.py:84-99`

Fetches `limit * 4` items then filters client-side for excluded subtypes. If many items are excluded, pagination breaks or returns fewer than `limit` items.

**Recommendation**: Filter in SQL using metadata JSON queries.

#### 2.8 Missing Error Case Tests (MEDIUM)

`test_api.py` doesn't test: malformed ZIP, oversized files, network failures, database errors, or LLM proxy failures.

#### 2.9 Hardcoded Tailscale Domain (MEDIUM)

**Files**: `app/src/index.html:658`, `packages/lestash-server/src/lestash_server/cli.py:18-19`

Personal Tailscale domain `pop-mini.monkey-ladon.ts.net` hardcoded in multiple places. Should be configurable.

---

## 3. Source Plugins

### Bluesky Plugin

#### 3.1 Plaintext Credential Storage (HIGH)

**File**: `packages/lestash-bluesky/src/lestash_bluesky/client.py:28-37`

Passwords and sessions stored in plaintext JSON files. `chmod(0o600)` restricts permissions but doesn't encrypt.

**Recommendation**: Use the `keyring` library or encrypt credentials at rest.

#### 3.2 Session Reuse Without Expiration Check (MEDIUM)

**File**: `packages/lestash-bluesky/src/lestash_bluesky/client.py:100-112`

Sessions loaded and reused without verifying token validity or expiration.

#### 3.3 Silent Sync Failures (HIGH)

**File**: `packages/lestash-bluesky/src/lestash_bluesky/source.py:405-407`

Entire sync returns without yielding on exception. Client never knows sync failed.

### YouTube Plugin

#### 3.4 No API Quota Tracking (HIGH)

**File**: `packages/lestash-youtube/src/lestash_youtube/client.py:147-274`

Unbounded API calls with no rate limiting or quota warnings. YouTube API has 10k units/day quota.

#### 3.5 Incomplete Pagination (MEDIUM)

**File**: `packages/lestash-youtube/src/lestash_youtube/client.py:186-248`

Playlist items are paginated but video detail fetches assume fewer than 50 items. Users with 100+ liked videos get incomplete data.

### LinkedIn Plugin

#### 3.6 Missing CSRF Validation in OAuth (HIGH)

**File**: `packages/lestash-linkedin/src/lestash_linkedin/api.py:134, 162`

State token generated but never validated in the OAuth callback handler. Vulnerable to CSRF attacks.

**Recommendation**: Store state token and verify it matches in the callback.

#### 3.7 No Token Refresh Mechanism (MEDIUM)

OAuth tokens expire but no refresh mechanism exists. Users will hit auth errors after token expiry.

### ArXiv Plugin

#### 3.8 Unbounded Results (LOW)

**File**: `packages/lestash-arxiv/src/lestash_arxiv/source.py:169`

No limit on total results across all keywords (keywords * max_per_keyword).

### Micro.blog Plugin

#### 3.9 No Token Validation on Init (LOW)

**File**: `packages/lestash-microblog/src/lestash_microblog/client.py:68-72`

No check if token is valid before making requests. First request fails without a helpful error.

### Cross-Plugin Issues

#### 3.10 No Plugin Timeout Context (HIGH)

All plugins lack timeout wrappers. Long-running syncs can block the server or CLI indefinitely.

**Recommendation**: Implement timeout wrapper or async execution with configurable limits.

#### 3.11 No Config Validation (MEDIUM)

`plugin.sync(plugin_config)` passes config dicts without schema validation. Plugins trust config has required keys.

**Recommendation**: Use Pydantic models for per-plugin config validation.

---

## 4. Frontend (`app/`)

### Security

#### 4.1 Unrestricted API Base URL (HIGH)

**File**: `app/src/index.html:677-679`

```javascript
function getApiBase() {
  if (IS_TAURI) return `https://${settings.host}:${settings.port}`;
  return '';
}
```

Settings come from localStorage (user-editable). No host validation. An attacker could redirect API calls via localStorage manipulation.

#### 4.2 Race Condition in Load More (MEDIUM)

**File**: `app/src/index.html:823-862`

`loadFeed(true)` can fire multiple times before previous request completes. No debouncing or loading state guard. Could produce duplicate items or broken pagination.

#### 4.3 Missing Error Boundary (MEDIUM)

Global error handler sets `document.title` but doesn't prevent UI from breaking. Should render visible error message or fallback UI.

#### 4.4 No Type Checking (LOW)

Single-file JavaScript embedded in HTML with no TypeScript or JSDoc type hints. Increases risk of type-related bugs.

---

## 5. Deployment (`deploy/`)

### Critical

#### 5.1 Hardcoded Paths in systemd Services (HIGH)

**File**: `deploy/lestash-server.service:7-11`

```ini
ExecStart=/home/linuxbrew/.linuxbrew/bin/uv run --project %h/Projects/thompsonson/lestash ...
Environment=LESTASH_TLS_CERT=%h/.config/tailscale-certs/pop-mini.monkey-ladon.ts.net.crt
```

Machine-specific paths and Tailscale domain hardcoded. Will fail on any other system.

**Recommendation**: Use EnvironmentFile or parameterize via config.

### Medium

#### 5.2 No Restart Backoff

`RestartSec=3` retries quickly. Should add `StartLimitIntervalSec=60` and `StartLimitBurst=3`.

#### 5.3 Sync Timer May Overlap

`OnUnitActiveSec=6h` could start a new sync while the previous is still running. Add `Type=oneshot` to the service.

#### 5.4 No Dependency Ordering

Sync service has no `After=lestash-server.service`. Could attempt sync before server is ready.

#### 5.5 Install Script Has No Validation

Installs systemd units without checking validity or backing up existing units.

---

## 6. Configuration and Dependencies

### Strengths

- Consistent version management across all 8 packages (1.18.3)
- Proper UV workspace with `[tool.uv.workspace]` members
- Comprehensive Justfile with lint, format, typecheck, and test targets
- CI tests against Python 3.12 and 3.13
- Pre-commit hooks configured with ruff and formatting
- Semantic release automation
- Dependabot configured for weekly updates
- Excellent Angular commit convention adherence

### Issues

#### 6.1 Inconsistent httpx Version Constraints (MEDIUM)

- `lestash-linkedin`: `httpx>=0.26.0`
- `lestash-microblog`: `httpx>=0.27.0`

**Recommendation**: Align to `httpx>=0.27.0` across all packages.

#### 6.2 Incomplete Test Coverage Configuration (MEDIUM)

**File**: `justfile`

`test-coverage` only runs for 3 of 7 packages (lestash, lestash-bluesky, lestash-linkedin). Missing arxiv, youtube, server, and microblog.

#### 6.3 Release Workflow Missing PyPI Token Validation (LOW)

No explicit check that PyPI credentials are available before publish step.

#### 6.4 Missing .gitignore Entry (LOW)

`app/dist/` (built frontend) not in `.gitignore`. Build outputs could be accidentally committed.

#### 6.5 Unused Ruff Rules (LOW)

Current selection: `["E", "F", "I", "UP", "B", "SIM"]`. Consider adding `"C901"` for complexity checking.

---

## 7. Recommendations Summary

### Immediate (Security - Fix Before Broader Deployment)

1. Add try-except around all `json.loads()` calls on database data
2. Add transaction rollback on sync failures
3. Restrict CORS to specific methods and headers
4. Validate file upload size before reading into memory
5. Encrypt credential storage in Bluesky plugin (use `keyring`)
6. Add CSRF validation to LinkedIn OAuth flow
7. Add timeouts to all external API calls in plugins
8. Remove hardcoded hostnames and paths from code and deployment files

### High Priority (Reliability)

9. Fix database connection context manager to rollback on error
10. Implement per-item error handling in sync loops
11. Fix insert vs update detection (rowcount issue)
12. Add YouTube API quota tracking
13. Fix YouTube pagination for large collections
14. Extract duplicate SQL into shared database functions
15. Enable proper CSP in Tauri configuration

### Medium Priority (Code Quality)

16. Sync version numbers between `__init__.py` and `pyproject.toml`
17. Adopt or remove custom exception classes
18. Add comprehensive error case tests
19. Validate plugin configurations with Pydantic models
20. Implement request debouncing in frontend
21. Align httpx version constraints across plugins
22. Extend test coverage to all packages
23. Add restart backoff and dependency ordering to systemd services

---

## 8. Architecture Notes

### What Works Well

- **Plugin system**: Clean entry-point registration via `[project.entry-points."lestash.sources"]`
- **Database design**: SQLite with FTS5, WAL mode, foreign keys, and versioned migrations
- **Monorepo structure**: UV workspace keeps packages independent but buildable together
- **CI/CD**: Multi-version Python testing, semantic release, dependabot
- **CLI UX**: Rich output, clear commands, good help text
- **Single-file frontend**: Pragmatic choice for a personal tool, works in both Tauri and browser

### Architectural Risks

- **No async in sync operations**: Plugin syncs are synchronous, blocking the event loop when called from the server
- **Single SQLite database**: Fine for personal use but becomes a bottleneck with concurrent access beyond WAL capacity
- **No retry/backoff**: External API failures (Bluesky, YouTube, LinkedIn, ArXiv) have no retry logic
- **No health checks**: Server has no `/health` endpoint for monitoring
