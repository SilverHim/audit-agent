#!/bin/bash
# 建属主角色与应用角色 (开发接口文档.md 第 9 节). POSTGRES_USER 是超级用户, 只在初始化时使用.
set -euo pipefail
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  --set=owner_password="$AUDIT_OWNER_PASSWORD" --set=app_password="$AUDIT_APP_PASSWORD" <<'SQL'
CREATE ROLE audit_owner LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD :'owner_password';
CREATE ROLE audit_app   LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD :'app_password';
CREATE DATABASE audit OWNER audit_owner;
SQL
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname audit <<SQL
ALTER SCHEMA public OWNER TO audit_owner;
GRANT USAGE ON SCHEMA public TO audit_app;
ALTER DEFAULT PRIVILEGES FOR ROLE audit_owner IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO audit_app;
ALTER DEFAULT PRIVILEGES FOR ROLE audit_owner IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO audit_app;
SQL
