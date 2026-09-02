-- Extensions.
--
-- PostGIS is mandatory: the registry's whole reason to exist beyond a
-- spreadsheet is that "where is the nearest working camera" is a spatial
-- question, and gap analysis is a spatial answer.
CREATE EXTENSION IF NOT EXISTS postgis;

-- TimescaleDB carries the health time-series. It is loaded conditionally
-- because the extension needs shared_preload_libraries set on the server, and a
-- plain postgis/postgis image does not have it.
--
-- Degrading rather than failing is deliberate: the registry must come up on any
-- Postgres a judge or a teammate happens to point it at. Without Timescale the
-- heartbeat table is a normal table with the same indexes and the same queries —
-- it simply stops compressing and stops dropping old chunks on its own.
DO $$
BEGIN
    CREATE EXTENSION IF NOT EXISTS timescaledb;
EXCEPTION WHEN OTHERS THEN
    RAISE WARNING
        'timescaledb unavailable (%): camera_heartbeat will be a plain table. '
        'Retention and compression must then be handled by a cron job.',
        SQLERRM;
END
$$;
