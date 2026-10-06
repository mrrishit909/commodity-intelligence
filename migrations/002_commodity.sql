-- Domain model. Names follow the blueprint's schema; additions are marked (+).

CREATE TABLE markets (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants UNIQUE, model_seed int NOT NULL, now_week int NOT NULL, start_date date NOT NULL, shocks jsonb NOT NULL DEFAULT '[]', created_at timestamptz NOT NULL DEFAULT now());   -- (+) seed and clock
SELECT enable_tenant_rls('markets');

CREATE TABLE commodities (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants, symbol text NOT NULL, name text NOT NULL, unit text NOT NULL, UNIQUE (tenant_id, symbol));
SELECT enable_tenant_rls('commodities');

CREATE TABLE instruments (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants, commodity_id uuid NOT NULL REFERENCES commodities, venue text NOT NULL, contract_code text NOT NULL, expiry date, currency text NOT NULL, UNIQUE (tenant_id, venue, contract_code));
SELECT enable_tenant_rls('instruments');

CREATE TABLE prices (instrument_id uuid NOT NULL REFERENCES instruments, ts timestamptz NOT NULL, open numeric, high numeric, low numeric, close numeric NOT NULL, volume numeric, tenant_id uuid NOT NULL REFERENCES tenants, week int NOT NULL,   -- (+) week
  PRIMARY KEY (instrument_id, ts));
SELECT enable_tenant_rls('prices');

CREATE TABLE fundamentals (commodity_id uuid NOT NULL REFERENCES commodities, region text NOT NULL, period date NOT NULL, metric text NOT NULL, value numeric NOT NULL, unit text NOT NULL, source text NOT NULL, tenant_id uuid NOT NULL REFERENCES tenants,
  week int NOT NULL, published_week int NOT NULL,   -- (+) the week the figure describes and the week it became available
  PRIMARY KEY (commodity_id, region, period, metric, source));
SELECT enable_tenant_rls('fundamentals');

CREATE TABLE vessel_flows (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants, commodity_id uuid NOT NULL REFERENCES commodities, vessel_id text NOT NULL, origin text NOT NULL, destination text NOT NULL, departed_at timestamptz NOT NULL, eta timestamptz NOT NULL, estimated_qty numeric NOT NULL,
  origin_region text NOT NULL, departed_week int NOT NULL, planned_eta_week int NOT NULL, arrival_week int NOT NULL);   -- (+) the mine region, week numbers, and when it really arrives (known only once it has)
SELECT enable_tenant_rls('vessel_flows');
CREATE INDEX vessel_flows_dep ON vessel_flows (tenant_id, departed_week);

CREATE TABLE events (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants, commodity_id uuid NOT NULL REFERENCES commodities, occurred_at timestamptz NOT NULL, event_type text NOT NULL, region text NOT NULL, severity numeric NOT NULL, source_uri text NOT NULL,
  detail jsonb NOT NULL DEFAULT '{}', recorded_by uuid NOT NULL, recorded_at timestamptz NOT NULL DEFAULT now());   -- (+) detail and provenance; the blueprint's embedding column is not built (no language model here)
SELECT enable_tenant_rls('events');

CREATE TABLE forecasts (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants, commodity_id uuid NOT NULL REFERENCES commodities, target text NOT NULL, horizon text NOT NULL, value numeric NOT NULL, lower numeric NOT NULL, upper numeric NOT NULL, model_version text NOT NULL, asof timestamptz NOT NULL,
  asof_week int NOT NULL, evidence jsonb NOT NULL DEFAULT '{}', run_id uuid NOT NULL);   -- (+) week, the signal contributions and drivers, the run that produced it
SELECT enable_tenant_rls('forecasts');
CREATE INDEX forecasts_target ON forecasts (tenant_id, target, horizon, asof_week);
