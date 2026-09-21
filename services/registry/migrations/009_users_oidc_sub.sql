-- OIDC subject binding for SSO accounts (services/bff, docs/KEYCLOAK.md).
--
-- The one column Keycloak account linking may key on. `sub` is the only claim
-- the IdP guarantees is stable and unique per realm user; `preferred_username`
-- is mutable realm data, and a link keyed on it lets a realm user named
-- `admin` silently inherit the builtin admin account. The BFF's callback
-- resolves `WHERE oidc_sub = $1` FIRST and never falls back to a username
-- match: a same-named existing row is a collision to refuse, not an account
-- to claim.
--
-- Nullable because builtin (password) users have no IdP subject — Postgres
-- UNIQUE treats NULLs as distinct, so the constraint costs those rows
-- nothing. The same table-ownership arrangement as 006_identity.sql applies:
-- the BFF is the only service that reads or writes `users`; the file lives
-- here because the registry's checksummed, advisory-locked runner owns every
-- migration in the shared database.

ALTER TABLE users ADD COLUMN oidc_sub text UNIQUE;
