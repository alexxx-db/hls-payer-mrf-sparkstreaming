# Databricks notebook source
import fnmatch
import hashlib
import json
from datetime import datetime, timezone

from pyspark.sql import types as T

# COMMAND ----------

dbutils.widgets.text("catalog", "main")
dbutils.widgets.text("schema", "hls_payer_transparency")
dbutils.widgets.text("volume", "payer_transparency")
dbutils.widgets.text("source_path", "")
dbutils.widgets.text("source_pattern", "*.json")
dbutils.widgets.text("max_files_per_run", "100")
dbutils.widgets.text("include_failed", "true")
dbutils.widgets.text("job_id", "")
dbutils.widgets.text("job_run_id", "")

catalog = dbutils.widgets.get("catalog")
schema = dbutils.widgets.get("schema")
volume = dbutils.widgets.get("volume")
source_path = dbutils.widgets.get("source_path") or f"/Volumes/{catalog}/{schema}/{volume}/raw_files"
source_pattern = dbutils.widgets.get("source_pattern") or "*.json"
max_files_per_run = int(dbutils.widgets.get("max_files_per_run") or "100")
include_failed = (dbutils.widgets.get("include_failed") or "true").lower() == "true"
job_id = dbutils.widgets.get("job_id")
job_run_id = dbutils.widgets.get("job_run_id")

# COMMAND ----------

def quote(identifier):
    return f"`{identifier.replace('`', '``')}`"


def table(name):
    return f"{quote(catalog)}.{quote(schema)}.{quote(name)}"


def sql_string(value):
    return "'" + (value or "").replace("'", "''") + "'"


manifest_table = table("mrf_file_manifest")
bronze_table = table("payer_transparency_ingest_bronze")
metrics_table = table("mrf_ingest_metrics")
errors_table = table("mrf_parse_errors")
quality_table = table("mrf_data_quality_results")
observability_log_table = table("mrf_observability_collection_log")
job_run_audit_table = table("mrf_job_run_audit")
job_task_run_audit_table = table("mrf_job_task_run_audit")
access_audit_table = table("mrf_access_audit_events")

spark.sql(f"CREATE SCHEMA IF NOT EXISTS {quote(catalog)}.{quote(schema)}")
spark.sql(f"CREATE VOLUME IF NOT EXISTS {quote(catalog)}.{quote(schema)}.{quote(volume)}")

# COMMAND ----------

spark.sql(f"""
CREATE TABLE IF NOT EXISTS {manifest_table} (
  file_id STRING NOT NULL,
  source_path STRING NOT NULL,
  source_file_name STRING,
  source_size_bytes BIGINT,
  source_modification_time TIMESTAMP,
  discovered_at TIMESTAMP,
  status STRING,
  attempts INT,
  last_run_id STRING,
  updated_at TIMESTAMP,
  error_class STRING,
  error_message STRING
) USING DELTA
TBLPROPERTIES (
  'delta.enableChangeDataFeed' = 'true',
  'quality' = 'control'
)
""")

spark.sql(f"""
CREATE TABLE IF NOT EXISTS {bronze_table} (
  file_id STRING NOT NULL,
  source_path STRING NOT NULL,
  file_name STRING,
  header_key STRING,
  start_offset BIGINT,
  end_offset BIGINT,
  json_payload STRING,
  ingest_run_id STRING,
  ingest_ts TIMESTAMP,
  job_id STRING,
  job_run_id STRING
) USING DELTA
TBLPROPERTIES (
  'delta.enableChangeDataFeed' = 'true',
  'quality' = 'bronze'
)
""")

spark.sql(f"""
CREATE TABLE IF NOT EXISTS {metrics_table} (
  ingest_run_id STRING,
  file_id STRING,
  source_path STRING,
  status STRING,
  started_at TIMESTAMP,
  ended_at TIMESTAMP,
  duration_seconds DOUBLE,
  source_size_bytes BIGINT,
  rows_written BIGINT,
  min_start_offset BIGINT,
  max_end_offset BIGINT,
  job_id STRING,
  job_run_id STRING,
  task_key STRING,
  error_class STRING,
  error_message STRING
) USING DELTA
TBLPROPERTIES ('quality' = 'metrics')
""")

spark.sql(f"""
CREATE TABLE IF NOT EXISTS {errors_table} (
  ingest_run_id STRING,
  file_id STRING,
  source_path STRING,
  failed_at TIMESTAMP,
  job_id STRING,
  job_run_id STRING,
  task_key STRING,
  error_class STRING,
  error_message STRING,
  stack_trace STRING
) USING DELTA
TBLPROPERTIES ('quality' = 'errors')
""")

spark.sql(f"""
CREATE TABLE IF NOT EXISTS {quality_table} (
  job_run_id STRING,
  check_name STRING,
  passed BOOLEAN,
  observed_value DOUBLE,
  threshold_value DOUBLE,
  details STRING,
  checked_at TIMESTAMP
) USING DELTA
TBLPROPERTIES ('quality' = 'quality_gates')
""")

spark.sql(f"""
CREATE TABLE IF NOT EXISTS {observability_log_table} (
  job_run_id STRING,
  source_name STRING,
  collected_at TIMESTAMP,
  status STRING,
  records_copied BIGINT,
  error_message STRING
) USING DELTA
TBLPROPERTIES ('quality' = 'observability')
""")

spark.sql(f"""
CREATE TABLE IF NOT EXISTS {job_run_audit_table} (
  captured_at TIMESTAMP,
  workspace_id STRING,
  job_id STRING,
  job_run_id STRING,
  run_type STRING,
  trigger_type STRING,
  result_state STRING,
  period_start_time TIMESTAMP,
  period_end_time TIMESTAMP,
  job_parameters MAP<STRING, STRING>
) USING DELTA
TBLPROPERTIES ('quality' = 'observability')
""")

spark.sql(f"""
CREATE TABLE IF NOT EXISTS {job_task_run_audit_table} (
  captured_at TIMESTAMP,
  workspace_id STRING,
  job_id STRING,
  job_run_id STRING,
  task_run_id STRING,
  task_key STRING,
  parent_run_id STRING,
  compute_ids ARRAY<STRING>,
  result_state STRING,
  period_start_time TIMESTAMP,
  period_end_time TIMESTAMP
) USING DELTA
TBLPROPERTIES ('quality' = 'observability')
""")

spark.sql(f"""
CREATE TABLE IF NOT EXISTS {access_audit_table} (
  captured_at TIMESTAMP,
  event_time TIMESTAMP,
  workspace_id STRING,
  service_name STRING,
  action_name STRING,
  user_identity STRING,
  request_params STRING,
  response STRING
) USING DELTA
TBLPROPERTIES ('quality' = 'observability')
""")

# COMMAND ----------

def entry_is_dir(entry):
    marker = getattr(entry, "isDir", None)
    return marker() if callable(marker) else entry.path.endswith("/")


def to_timestamp_millis(value):
    if not value:
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc)


def discover_files(root_path, pattern):
    stack = [root_path]
    discovered = []
    while stack:
        current = stack.pop()
        for entry in dbutils.fs.ls(current):
            if entry_is_dir(entry):
                stack.append(entry.path)
                continue
            if not fnmatch.fnmatch(entry.name, pattern):
                continue
            size = int(getattr(entry, "size", 0) or 0)
            modification_ms = getattr(entry, "modificationTime", None)
            identity = f"{entry.path}|{size}|{modification_ms or ''}"
            discovered.append({
                "file_id": hashlib.sha256(identity.encode("utf-8")).hexdigest(),
                "source_path": entry.path,
                "source_file_name": entry.name,
                "source_size_bytes": size,
                "source_modification_time": to_timestamp_millis(modification_ms),
                "discovered_at": datetime.now(timezone.utc),
            })
    return discovered


file_schema = T.StructType([
    T.StructField("file_id", T.StringType(), False),
    T.StructField("source_path", T.StringType(), False),
    T.StructField("source_file_name", T.StringType(), True),
    T.StructField("source_size_bytes", T.LongType(), True),
    T.StructField("source_modification_time", T.TimestampType(), True),
    T.StructField("discovered_at", T.TimestampType(), True),
])

discovered_files = discover_files(source_path, source_pattern)
incoming_df = spark.createDataFrame(discovered_files, file_schema)
incoming_df.createOrReplaceTempView("incoming_mrf_files")

spark.sql(f"""
MERGE INTO {manifest_table} AS target
USING incoming_mrf_files AS source
ON target.file_id = source.file_id
WHEN MATCHED AND target.status IN ('FAILED', 'QUARANTINED') THEN UPDATE SET
  source_path = source.source_path,
  source_file_name = source.source_file_name,
  source_size_bytes = source.source_size_bytes,
  source_modification_time = source.source_modification_time,
  discovered_at = source.discovered_at,
  status = 'PENDING',
  updated_at = current_timestamp(),
  error_class = NULL,
  error_message = NULL
WHEN NOT MATCHED THEN INSERT (
  file_id,
  source_path,
  source_file_name,
  source_size_bytes,
  source_modification_time,
  discovered_at,
  status,
  attempts,
  last_run_id,
  updated_at,
  error_class,
  error_message
) VALUES (
  source.file_id,
  source.source_path,
  source.source_file_name,
  source.source_size_bytes,
  source.source_modification_time,
  source.discovered_at,
  'PENDING',
  0,
  NULL,
  current_timestamp(),
  NULL,
  NULL
)
""")

# Recover abandoned processing rows from interrupted runs.
spark.sql(f"""
UPDATE {manifest_table}
SET status = 'PENDING',
    updated_at = current_timestamp(),
    error_class = 'ABANDONED_PROCESSING',
    error_message = 'Reset from PROCESSING after 12 hours without completion'
WHERE status = 'PROCESSING'
  AND updated_at < current_timestamp() - INTERVAL 12 HOURS
""")

eligible = "'PENDING'" + (", 'FAILED'" if include_failed else "")
selected_rows = spark.sql(f"""
SELECT file_id, source_path
FROM {manifest_table}
WHERE status IN ({eligible})
ORDER BY discovered_at, source_path
LIMIT {max_files_per_run}
""").collect()

files_json = json.dumps([row.asDict() for row in selected_rows], separators=(",", ":"))
dbutils.jobs.taskValues.set(key="files_json", value=files_json)
dbutils.jobs.taskValues.set(key="file_count", value=str(len(selected_rows)))

print(f"Discovered {len(discovered_files)} matching files under {source_path}")
print(f"Queued {len(selected_rows)} files for job_run_id={job_run_id}")
