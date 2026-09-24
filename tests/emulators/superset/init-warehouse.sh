#!/bin/sh

set -eu

psql \
    --set=ON_ERROR_STOP=1 \
    --set=dashboard_password="$HEINZEL_SUPERSET_WAREHOUSE_PASSWORD" \
    --username "$POSTGRES_USER" \
    --dbname "$POSTGRES_DB" <<'SQL'
CREATE ROLE superset_reader LOGIN PASSWORD :'dashboard_password';
CREATE SCHEMA analytics;
CREATE TABLE analytics.orders_current (
    region text NOT NULL,
    revenue numeric(12, 2) NOT NULL
);
INSERT INTO analytics.orders_current VALUES ('east', 99.00), ('west', 30.00);
CREATE TABLE analytics.orders_isolated (LIKE analytics.orders_current INCLUDING ALL);
INSERT INTO analytics.orders_isolated VALUES ('north', 55.00);
GRANT CONNECT ON DATABASE heinzel_warehouse TO superset_reader;
GRANT USAGE ON SCHEMA analytics TO superset_reader;
GRANT SELECT ON analytics.orders_current TO superset_reader;
GRANT SELECT ON analytics.orders_isolated TO superset_reader;
SQL
