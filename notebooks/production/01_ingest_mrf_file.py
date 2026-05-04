# Databricks notebook source
import traceback
from datetime import datetime, timezone

from pyspark.sql import functions as F
from pyspark.sql import types as T

# COMMAND ----------

dbutils.widgets.text("catalog", "main")
dbutils.widgets.text("schema", "hls_payer_transparency")
dbutils.widgets.text("volume", "payer_transparency")
dbutils.widgets.text("file_id", "")
dbutils.widgets.text("source_path", "")
dbutils.widgets.text("buffer_size", "67108864")
dbutils.widgets.text("job_id", "")
dbutils.widgets.text("job_run_id", "")
dbutils.widgets.text("task_key", "process_file")

catalog = dbutils.widgets.get("catalog")
schema = dbutils.widgets.get("schema")
volume = dbutils.widgets.get("volume")
file_id = dbutils.widgets.get("file_id")
source_path = dbutils.widgets.get("source_path")
buffer_size = int(dbutils.widgets.get("buffer_size") or "67108864")
job_id = dbutils.widgets.get("job_id")
job_run_id = dbutils.widgets.get("job_run_id")
task_key = dbutils.widgets.get("task_key") or "process_file"

if not file_id or not source_path:
    raise ValueError("Both file_id and source_path are required.")

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
checkpoint_path = f"/Volumes/{catalog}/{schema}/{volume}/checkpoints/bronze/{file_id}"
ingest_run_id = f"{job_run_id or 'manual'}:{file_id}"
started_at = datetime.now(timezone.utc)

metric_schema = T.StructType([
    T.StructField("ingest_run_id", T.StringType(), True),
    T.StructField("file_id", T.StringType(), True),
    T.StructField("source_path", T.StringType(), True),
    T.StructField("status", T.StringType(), True),
    T.StructField("started_at", T.TimestampType(), True),
    T.StructField("ended_at", T.TimestampType(), True),
    T.StructField("duration_seconds", T.DoubleType(), True),
    T.StructField("source_size_bytes", T.LongType(), True),
    T.StructField("rows_written", T.LongType(), True),
    T.StructField("min_start_offset", T.LongType(), True),
    T.StructField("max_end_offset", T.LongType(), True),
    T.StructField("job_id", T.StringType(), True),
    T.StructField("job_run_id", T.StringType(), True),
    T.StructField("task_key", T.StringType(), True),
    T.StructField("error_class", T.StringType(), True),
    T.StructField("error_message", T.StringType(), True),
])

error_schema = T.StructType([
    T.StructField("ingest_run_id", T.StringType(), True),
    T.StructField("file_id", T.StringType(), True),
    T.StructField("source_path", T.StringType(), True),
    T.StructField("failed_at", T.TimestampType(), True),
    T.StructField("job_id", T.StringType(), True),
    T.StructField("job_run_id", T.StringType(), True),
    T.StructField("task_key", T.StringType(), True),
    T.StructField("error_class", T.StringType(), True),
    T.StructField("error_message", T.StringType(), True),
    T.StructField("stack_trace", T.StringType(), True),
])

# COMMAND ----------

def append_metric(status, ended_at, rows_written=None, min_start_offset=None, max_end_offset=None, error_class=None, error_message=None):
    duration = (ended_at - started_at).total_seconds()
    source_size = spark.sql(f"""
      SELECT source_size_bytes
      FROM {manifest_table}
      WHERE file_id = {sql_string(file_id)}
      LIMIT 1
    """).first()
    source_size_bytes = int(source_size.source_size_bytes) if source_size and source_size.source_size_bytes is not None else None
    record = [{
        "ingest_run_id": ingest_run_id,
        "file_id": file_id,
        "source_path": source_path,
        "status": status,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_seconds": duration,
        "source_size_bytes": source_size_bytes,
        "rows_written": rows_written,
        "min_start_offset": min_start_offset,
        "max_end_offset": max_end_offset,
        "job_id": job_id,
        "job_run_id": job_run_id,
        "task_key": task_key,
        "error_class": error_class,
        "error_message": error_message,
    }]
    spark.createDataFrame(record, metric_schema).write.mode("append").saveAsTable(metrics_table)


def append_error(error_class, error_message, stack_trace):
    record = [{
        "ingest_run_id": ingest_run_id,
        "file_id": file_id,
        "source_path": source_path,
        "failed_at": datetime.now(timezone.utc),
        "job_id": job_id,
        "job_run_id": job_run_id,
        "task_key": task_key,
        "error_class": error_class,
        "error_message": error_message,
        "stack_trace": stack_trace,
    }]
    spark.createDataFrame(record, error_schema).write.mode("append").saveAsTable(errors_table)


def update_manifest(status, error_class=None, error_message=None):
    spark.sql(f"""
      UPDATE {manifest_table}
      SET status = {sql_string(status)},
          last_run_id = {sql_string(ingest_run_id)},
          updated_at = current_timestamp(),
          error_class = {sql_string(error_class) if error_class else "NULL"},
          error_message = {sql_string(error_message) if error_message else "NULL"}
      WHERE file_id = {sql_string(file_id)}
    """)


def mark_processing():
    spark.sql(f"""
      UPDATE {manifest_table}
      SET status = 'PROCESSING',
          attempts = coalesce(attempts, 0) + 1,
          last_run_id = {sql_string(ingest_run_id)},
          updated_at = current_timestamp(),
          error_class = NULL,
          error_message = NULL
      WHERE file_id = {sql_string(file_id)}
    """)

# COMMAND ----------

try:
    mark_processing()

    # Make retry/replay deterministic for this file.
    spark.sql(f"DELETE FROM {bronze_table} WHERE file_id = {sql_string(file_id)}")
    dbutils.fs.rm(checkpoint_path, True)

    parsed_df = (
        spark.readStream
        .format("payer-mrf")
        .option("buffersize", str(buffer_size))
        .option("includeOffsets", "true")
        .load(source_path)
    )

    bronze_df = (
        parsed_df
        .withColumn("file_id", F.lit(file_id))
        .withColumn("source_path", F.lit(source_path))
        .withColumn("ingest_run_id", F.lit(ingest_run_id))
        .withColumn("ingest_ts", F.current_timestamp())
        .withColumn("job_id", F.lit(job_id))
        .withColumn("job_run_id", F.lit(job_run_id))
        .select(
            "file_id",
            "source_path",
            "file_name",
            "header_key",
            "start_offset",
            "end_offset",
            "json_payload",
            "ingest_run_id",
            "ingest_ts",
            "job_id",
            "job_run_id",
        )
    )

    query = (
        bronze_df.writeStream
        .format("delta")
        .outputMode("append")
        .option("checkpointLocation", checkpoint_path)
        .trigger(availableNow=True)
        .queryName(f"payer_mrf_ingest_{file_id[:12]}")
        .table(bronze_table)
    )
    query.awaitTermination()

    summary = spark.sql(f"""
      SELECT
        count(*) AS rows_written,
        min(start_offset) AS min_start_offset,
        max(end_offset) AS max_end_offset
      FROM {bronze_table}
      WHERE file_id = {sql_string(file_id)}
    """).first()

    rows_written = int(summary.rows_written or 0)
    min_start_offset = int(summary.min_start_offset) if summary.min_start_offset is not None else None
    max_end_offset = int(summary.max_end_offset) if summary.max_end_offset is not None else None

    update_manifest("SUCCEEDED")
    append_metric("SUCCEEDED", datetime.now(timezone.utc), rows_written, min_start_offset, max_end_offset)
    dbutils.jobs.taskValues.set(key="rows_written", value=str(rows_written))
    print(f"Ingested file_id={file_id} rows={rows_written} path={source_path}")

except Exception as exc:
    error_class = exc.__class__.__name__
    error_message = str(exc)[:4000]
    stack_trace = traceback.format_exc()
    update_manifest("FAILED", error_class, error_message)
    append_error(error_class, error_message, stack_trace)
    append_metric("FAILED", datetime.now(timezone.utc), error_class=error_class, error_message=error_message)
    raise
