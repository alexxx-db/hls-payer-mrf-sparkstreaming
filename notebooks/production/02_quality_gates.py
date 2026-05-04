# Databricks notebook source
from datetime import datetime, timezone

from pyspark.sql import types as T

# COMMAND ----------

dbutils.widgets.text("catalog", "main")
dbutils.widgets.text("schema", "hls_payer_transparency")
dbutils.widgets.text("job_run_id", "")
dbutils.widgets.text("queued_file_count", "0")
dbutils.widgets.text("max_processing_age_hours", "12")

catalog = dbutils.widgets.get("catalog")
schema = dbutils.widgets.get("schema")
job_run_id = dbutils.widgets.get("job_run_id")
queued_file_count = int(dbutils.widgets.get("queued_file_count") or "0")
max_processing_age_hours = int(dbutils.widgets.get("max_processing_age_hours") or "12")

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
quality_table = table("mrf_data_quality_results")

quality_schema = T.StructType([
    T.StructField("job_run_id", T.StringType(), True),
    T.StructField("check_name", T.StringType(), True),
    T.StructField("passed", T.BooleanType(), True),
    T.StructField("observed_value", T.DoubleType(), True),
    T.StructField("threshold_value", T.DoubleType(), True),
    T.StructField("details", T.StringType(), True),
    T.StructField("checked_at", T.TimestampType(), True),
])

# COMMAND ----------

def scalar(query):
    return spark.sql(query).first()[0]


def add_check(results, check_name, observed_value, threshold_value, passed, details):
    results.append({
        "job_run_id": job_run_id,
        "check_name": check_name,
        "passed": bool(passed),
        "observed_value": float(observed_value or 0),
        "threshold_value": float(threshold_value or 0),
        "details": details,
        "checked_at": datetime.now(timezone.utc),
    })


job_filter = f"job_run_id = {sql_string(job_run_id)}" if job_run_id else "job_run_id IS NULL"
results = []

failed_files = scalar(f"""
SELECT count(*)
FROM {metrics_table}
WHERE {job_filter}
  AND status = 'FAILED'
""")
add_check(results, "no_failed_file_ingestions", failed_files, 0, failed_files == 0, "No file task in this job run may fail.")

successful_zero_row_files = scalar(f"""
SELECT count(*)
FROM {metrics_table}
WHERE {job_filter}
  AND status = 'SUCCEEDED'
  AND coalesce(rows_written, 0) = 0
""")
add_check(results, "successful_files_write_rows", successful_zero_row_files, 0, successful_zero_row_files == 0, "Successful file tasks must write at least one bronze row.")

invalid_offsets = scalar(f"""
SELECT count(*)
FROM {bronze_table}
WHERE {job_filter}
  AND (start_offset IS NULL OR end_offset IS NULL OR end_offset < start_offset)
""")
add_check(results, "valid_bronze_offsets", invalid_offsets, 0, invalid_offsets == 0, "Bronze rows must carry valid persisted byte offsets.")

duplicate_offsets = scalar(f"""
SELECT count(*)
FROM (
  SELECT file_id, start_offset, end_offset, count(*) AS row_count
  FROM {bronze_table}
  WHERE {job_filter}
  GROUP BY file_id, start_offset, end_offset
  HAVING count(*) > 1
)
""")
add_check(results, "no_duplicate_file_offsets", duplicate_offsets, 0, duplicate_offsets == 0, "A file offset range should be loaded once per replay.")

stale_processing = scalar(f"""
SELECT count(*)
FROM {manifest_table}
WHERE status = 'PROCESSING'
  AND updated_at < current_timestamp() - INTERVAL {max_processing_age_hours} HOURS
""")
add_check(results, "no_stale_processing_files", stale_processing, 0, stale_processing == 0, "PROCESSING manifest entries older than the threshold need operator attention.")

metric_rows = scalar(f"""
SELECT count(*)
FROM {metrics_table}
WHERE {job_filter}
""")
expected_metric_rows = queued_file_count
add_check(
    results,
    "metrics_emitted",
    metric_rows,
    expected_metric_rows,
    metric_rows >= expected_metric_rows,
    "Each queued file should emit an ingestion metric; zero-file runs are allowed."
)

quality_df = spark.createDataFrame(results, quality_schema)
quality_df.write.mode("append").saveAsTable(quality_table)

failures = [result for result in results if not result["passed"]]
dbutils.jobs.taskValues.set(key="quality_failures", value=str(len(failures)))

if failures:
    failure_summary = "; ".join(f"{item['check_name']}={item['observed_value']}" for item in failures)
    raise RuntimeError(f"MRF production quality gates failed: {failure_summary}")

print("All MRF production quality gates passed.")
