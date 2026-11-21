-- Apply after Django migrations, as the owner of these tables.
GRANT USAGE ON SCHEMA public TO sma_recorder;
GRANT SELECT (id, stream_id, name, environment_id, unit_id)
  ON portal_channel TO sma_recorder;
GRANT SELECT (stream_id, channel_id, slot)
  ON portal_channelinput TO sma_recorder;
GRANT SELECT (id, slug, name)
  ON portal_environment TO sma_recorder;
GRANT SELECT (id, name)
  ON portal_unit TO sma_recorder;
