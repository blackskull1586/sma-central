GRANT USAGE ON SCHEMA public TO sma_domains;
GRANT SELECT (id, hostname, enabled, status, detail, checked_at, updated_at)
  ON portal_customdomain TO sma_domains;
GRANT UPDATE (status, detail, checked_at)
  ON portal_customdomain TO sma_domains;
