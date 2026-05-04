# Databricks notebook source
from datetime import datetime, timezone

from pyspark.sql import types as T

# COMMAND ----------

dbutils.widgets.text("catalog", "main")
dbutils.widgets.text("schema", "hls_payer_transparency")
dbutils.widgets.text("job_id", "")
dbutils.widgets.text("job_run_id", "")
dbutils.widgets.text("collect_system_tables", "true")

catalog = dbutils.widgets.get("catalog")
schema = dbutils.widgets.get("schema")
job_id = dbutils.widgets.get("job_id")
job_run_id = dbutils.widgets.get("job_run_id")
collect_system_tables = (dbutils.widgets.get("collect_system_tables") or "true").lower() == "true"

# COMMAND ----------

def quote(identifier):
    return f"`{identifier.replace('`', '``')}`"


def table(name):
    return f"{quote(catalog)}.{quote(schema)}.{quote(name)}"


def sql_string(value):
    return "'" + (value or "").replace("'", "''") + "'"


metrics_table = table("mrf_ingest_metrics")
errors_table = table("mrf_parse_errors")
quality_table = table("mrf_data_quality_results")
observability_log_table = table("mrf_observability_collection_log")
job_run_audit_table = table("mrf_job_run_audit")
job_task_run_audit_table = table("mrf_job_task_run_audit")
access_audit_table = table("mrf_access_audit_events")

log_schema = T.StructType([
    T.StructField("job_run_id", T.StringType(), True),
    T.StructField("source_name", T.StringType(), True),
    T.StructField("collected_at", T.TimestampType(), True),
    T.StructField("status", T.StringType(), True),
    T.StructField("records_copied", T.LongType(), True),
    T.StructField("error_message", T.StringType(), True),
])

# COMMAND ----------

def log_collection(source_name, status, records_copied=0, error_message=None):
    record = [{
        "job_run_id": job_run_id,
        "source_name": source_name,
        "collected_at": datetime.now(timezone.utc),
        "status": status,
        "records_copied": int(records_copied or 0),
        "error_message": error_message[:4000] if error_message else None,
    }]
    spark.createDataFrame(record, log_schema).write.mode("append").saveAsTable(observability_log_table)


def append_query_to_table(source_name, query, target_table):
    try:
        df = spark.sql(query)
        rows = df.count()
        if rows:
            df.write.mode("append").saveAsTable(target_table)
        log_collection(source_name, "SUCCEEDED", rows)
    except Exception as exc:
        log_collection(source_name, "SKIPPED", 0, str(exc))

# COMMAND ----------

spark.sql(f"""
CREATE OR REPLACE VIEW {table("mrf_ingestion_observability_v")} AS
SELECT
  date_trunc('day', started_at) AS ingest_day,
  status,
  count(*) AS file_tasks,
  sum(coalesce(rows_written, 0)) AS rows_written,
  sum(coalesce(source_size_bytes, 0)) AS source_size_bytes,
  avg(duration_seconds) AS avg_duration_seconds,
  max(duration_seconds) AS max_duration_seconds
FROM {metrics_table}
GROUP BY date_trunc('day', started_at), status
""")

spark.sql(f"""
CREATE OR REPLACE VIEW {table("mrf_latest_failures_v")} AS
SELECT
  failed_at,
  job_id,
  job_run_id,
  task_key,
  file_id,
  source_path,
  error_class,
  error_message
FROM {errors_table}
WHERE failed_at >= current_timestamp() - INTERVAL 7 DAYS
""")

spark.sql(f"""
CREATE OR REPLACE VIEW {table("mrf_latest_quality_v")} AS
SELECT
  job_run_id,
  check_name,
  passed,
  observed_value,
  threshold_value,
  details,
  checked_at
FROM {quality_table}
WHERE checked_at >= current_timestamp() - INTERVAL 7 DAYS
""")

log_collection("custom_observability_views", "SUCCEEDED", 3)

# COMMAND ----------

if collect_system_tables:
    append_query_to_table(
        "system.lakeflow.job_run_timeline",
        f"""
        SELECT
          current_timestamp() AS captured_at,
          cast(workspace_id AS STRING) AS workspace_id,
          cast(job_id AS STRING) AS job_id,
          cast(run_id AS STRING) AS job_run_id,
          cast(run_type AS STRING) AS run_type,
          cast(trigger_type AS STRING) AS trigger_type,
          cast(result_state AS STRING) AS result_state,
          period_start_time,
          period_end_time,
          job_parameters
        FROM system.lakeflow.job_run_timeline
        WHERE cast(job_id AS STRING) = {sql_string(job_id)}
          AND cast(run_id AS STRING) = {sql_string(job_run_id)}
        """,
        job_run_audit_table,
    )

    append_query_to_table(
        "system.lakeflow.job_task_run_timeline",
        f"""
        SELECT
          current_timestamp() AS captured_at,
          cast(workspace_id AS STRING) AS workspace_id,
          cast(job_id AS STRING) AS job_id,
          cast(run_id AS STRING) AS job_run_id,
          cast(task_run_id AS STRING) AS task_run_id,
          cast(task_key AS STRING) AS task_key,
          cast(parent_run_id AS STRING) AS parent_run_id,
          compute_ids,
          cast(result_state AS STRING) AS result_state,
          period_start_time,
          period_end_time
        FROM system.lakeflow.job_task_run_timeline
        WHERE cast(job_id AS STRING) = {sql_string(job_id)}
          AND cast(run_id AS STRING) = {sql_string(job_run_id)}
        """,
        job_task_run_audit_table,
    )

    append_query_to_table(
        "system.access.audit",
        f"""
        SELECT
          current_timestamp() AS captured_at,
          event_time,
          cast(workspace_id AS STRING) AS workspace_id,
          service_name,
          action_name,
          to_json(user_identity) AS user_identity,
          to_json(request_params) AS request_params,
          to_json(response) AS response
        FROM system.access.audit
        WHERE event_time >= current_timestamp() - INTERVAL 1 DAY
          AND (
            cast(request_params AS STRING) LIKE concat('%', {sql_string(catalog)}, '%')
            OR cast(request_params AS STRING) LIKE concat('%', {sql_string(schema)}, '%')
          )
        """,
        access_audit_table,
    )
else:
    log_collection("system_tables", "SKIPPED", 0, "collect_system_tables=false")

print("Published MRF observability views and attempted system table collection.")
