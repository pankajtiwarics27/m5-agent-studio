# Typed configuration and versioned contracts

## Configuration model

The process-start environment is parsed by `load_settings()` into the frozen, typed `StudioSettings` dataclass in `app.py`. The existing module-level names (`STUDIO_HOST`, `STUDIO_PORT`, `DB_PATH`, and others) remain typed aliases for compatibility with the standalone build and existing integrations. Settings are loaded once when the application module is imported; invalid input raises `ConfigurationError` naming the setting and expected form, without echoing secret values. The API token is excluded from the settings object's generated representation. The loader is pure with respect to external services: it does not contact a provider, read a database, or execute a command.

There is no implicit `.env`, YAML, or JSON configuration file. For process settings, **an environment value overrides the code default**; a missing or empty boolean uses its default for compatibility. Boolean values are case-insensitive `1`, `true`, `yes`, `on`, `0`, `false`, `no`, or `off`; other non-empty values are rejected rather than silently interpreted as false. Numeric settings require integers and are range checked:

| Setting | Contract |
|---|---|
| `STUDIO_PORT` | Integer from 1 through 65535. |
| `STUDIO_MAX_REQUEST_BYTES` | Integer from 1024 through 104857600 (100 MiB). |
| `STUDIO_EXPENSIVE_REQUESTS_PER_MINUTE` | Integer from 1 through 600. |
| `STUDIO_API_TOKEN` | Empty only for the loopback-generated-token path; otherwise at least 32 non-whitespace characters. |
| `STUDIO_EXECUTION_MODE` | `disabled`, `sandbox`, or `host`; `host` requires `STUDIO_ALLOW_HOST_EXECUTION=1`. If the mode is omitted, the legacy host-execution opt-in still selects `host`, preserving its documented behavior. |
| `STUDIO_AUTH_DISABLED` | Permitted only for a loopback `STUDIO_HOST`; non-loopback binds require an explicit token. |
| `STUDIO_EXTERNAL_STATIC` | Boolean used by the standalone wrapper to read `static/index.html` instead of the embedded UI when enabled. |
| `OMLX_BASE_URL` | Absolute HTTP(S) URL with a hostname and valid port; credentials, query, and fragment are rejected. Private-network authorization is still enforced at outbound request time. |
| `STUDIO_ALLOWED_ORIGINS` | Comma-separated HTTP(S) origins, with no credentials, non-root path, query, or fragment; an optional trailing slash is normalized away. |
| `STUDIO_ALLOWED_HOSTS` | Non-empty comma-separated Host values without whitespace. |
| `STUDIO_DB_PATH`, `STUDIO_API_TOKEN_FILE` | Non-empty paths when explicitly set; `~` expansion is supported. |

Other optional safety flags use the same strict boolean parsing. Path and collection values remain environment-driven; the loader does not infer a model, provider, chip, memory size, or platform.

## Precedence contracts

1. **Process settings:** an explicit environment value wins over its code default. For example, `STUDIO_DB_PATH` overrides the source-adjacent default database path, while `STUDIO_WORKSPACE` overrides the current working directory.
2. **API token:** an explicit `STUDIO_API_TOKEN` is used instead of reading or generating the local token file. If it is absent on loopback and authentication is enabled, Studio reads or creates `STUDIO_API_TOKEN_FILE`. A non-loopback bind does not fall back to that local token file; it requires an explicit environment token.
3. **Provider credentials:** an existing provider value stored through the database/keyring reference takes precedence over that provider's environment-key fallback. An environment key can supply the effective credential when no stored value exists, and startup seeds a provider row from the environment only when that alias has no row. Saving a blank masked key preserves an existing stored value; the explicit clear action clears the stored value, after which an exported environment key can again be the fallback.
4. **Database/workspace paths:** explicit path settings take precedence over their defaults; the database and workspace paths are independent settings.
5. **Database maintenance CLI:** an explicit `--database` argument overrides the `STUDIO_DB_PATH`-derived default for backup/restore commands.

These are application contracts, not shell precedence rules. The application does not merge multiple config files or silently override a stored provider secret with an environment variable.

## Versioned SQLite schema contract

The current database contract is `SCHEMA_VERSION = 2`, mirrored by `PRAGMA user_version` and rows in `schema_migrations`. Migrations are additive and run inside `BEGIN IMMEDIATE`; existing rows and stored values must be preserved. Re-running initialization is idempotent. A database with a future `user_version` is rejected before schema creation or mutation so an older Studio cannot downgrade a newer database. A migration failure rolls back its transactional DDL and version update.

AS-007 and AS-009 do not change tables or columns and therefore keep the schema version at 2. Regression evidence includes the existing `test_versioned_schema_upgrade_adds_columns_to_legacy_tables`, `test_future_database_schema_fails_without_mutating`, and secret migration tests, plus version-1 idempotence, transaction rollback, and backup-recovery checks in `tests/test_security_and_quality.py`. See [`docs/storage-migrations.md`](storage-migrations.md) for the storage seam and explicit restore procedure.

## Validation and evidence

Run the ordinary tests with `python -m pytest -q`. The configuration loader is directly tested with isolated environment mappings for defaults, overrides, precedence, type/range errors, and unsafe combinations in `tests/test_configuration_contracts.py`. No live provider, MCP endpoint, or container runtime is required for those tests. Full runtime and schema compatibility remain covered by the existing local-only tests; no external deployment matrix or new platform claim is implied.
