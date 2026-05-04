# Databricks notebook source
# MAGIC %md This notebook is available at https://github.com/databricks-industry-solutions/hls-payer-mrf-sparkstreaming. For more information about this solution accelerator, visit https://www.databricks.com/solutions/accelerators/price-transparency-data.

# COMMAND ----------

# MAGIC %md 
# MAGIC ## Example Workflow Steps 
# MAGIC > **Bronze**
# MAGIC >> Download & Decompress   
# MAGIC >> Stream Data  
# MAGIC
# MAGIC > **Silver**  
# MAGIC >> Curation ETL into desired Data Model    
# MAGIC  
# MAGIC > **Gold** 
# MAGIC >> Query Meeting 2023, 2024 Price Comparison CMS Mandate

# COMMAND ----------

dbutils.widgets.text("catalog", "main")
dbutils.widgets.text("schema", "hls_dev_payer_transparency")
dbutils.widgets.text("volume", "payer_transparency")

catalog = dbutils.widgets.get("catalog")
schema = dbutils.widgets.get("schema")
volume = dbutils.widgets.get("volume")

def quoted(identifier):
  return f"`{identifier.replace('`', '``')}`"

qualified_schema = f"{quoted(catalog)}.{quoted(schema)}"

def table_name(name):
  return f"{qualified_schema}.{quoted(name)}"

source_file = "2022-12-01_UMR--Inc-_Third-Party-Administrator_ENCORE-ENTERPRISES-AIRROSTI-DCI_TX-DALLAS-NON-EVALUATED-GAP_-ENC_NXBJ_in-network-rates.json"
source_url = f"https://uhc-tic-mrf.azureedge.net/public-mrf/2022-12-01/{source_file}.gz"
raw_files_path = f"/Volumes/{catalog}/{schema}/{volume}/raw_files"
checkpoint_root = f"/Volumes/{catalog}/{schema}/{volume}/checkpoints"
source_data = f"{raw_files_path}/{source_file}"
source_gzip = f"{source_data}.gz"
checkpoint_path = f"{checkpoint_root}/payer_transparency_ingest"

ingest_table = table_name("payer_transparency_ingest")
provider_header_table = table_name("payer_transparency_in_network_provider_header")
provider_references_table = table_name("payer_transparency_in_network_provider_references")
in_network_table = table_name("payer_transparency_in_network_in_network")
provider_x_payer_table = table_name("payer_transparency_in_network_provider_references_x_payer")
codes_table = table_name("payer_transparency_in_network_in_network_codes")
rates_table = table_name("payer_transparency_in_network_in_network_rates")
prices_table = table_name("payer_transparency_in_network_in_network_rates_prices")
par_providers_table = table_name("payer_transparency_in_network_in_network_rates_par_providers")

spark.sql(f"CREATE SCHEMA IF NOT EXISTS {qualified_schema}")
spark.sql(f"CREATE VOLUME IF NOT EXISTS {qualified_schema}.{quoted(volume)}")
dbutils.fs.mkdirs(raw_files_path)
dbutils.fs.mkdirs(checkpoint_root)

# COMMAND ----------

# MAGIC %md
# MAGIC ### Bronze (Download, Unzip, and Parse via SparkStreaming )

# COMMAND ----------

# MAGIC %md
# MAGIC ![logo](https://github.com/databricks-industry-solutions/hls-payer-mrf-sparkstreaming/blob/main/img/bronze1.png?raw=true)

# COMMAND ----------

import urllib.request

local_gzip = f"/tmp/{source_file}.gz"
urllib.request.urlretrieve(source_url, local_gzip)
dbutils.fs.cp(f"file:{local_gzip}", source_gzip, True)

# COMMAND ----------

# MAGIC %md
# MAGIC ![logo](https://github.com/databricks-industry-solutions/hls-payer-mrf-sparkstreaming/blob/main/img/bronze2.png?raw=true)

# COMMAND ----------

import gzip
import shutil

local_json = f"/tmp/{source_file}"
with gzip.open(local_gzip, "rb") as compressed, open(local_json, "wb") as uncompressed:
  shutil.copyfileobj(compressed, uncompressed)

dbutils.fs.cp(f"file:{local_json}", source_data, True)

# COMMAND ----------

# Reinitialize tables to be created in this notebook
for table in [
  ingest_table,
  provider_header_table,
  provider_references_table,
  in_network_table,
  provider_x_payer_table,
  codes_table,
  rates_table,
  prices_table,
  par_providers_table,
]:
  spark.sql(f"DROP TABLE IF EXISTS {table}")

dbutils.fs.rm(checkpoint_path, True)

# COMMAND ----------

# MAGIC %md
# MAGIC ![logo](https://github.com/databricks-industry-solutions/hls-payer-mrf-sparkstreaming/blob/main/img/bronze3.1.png?raw=True)

# COMMAND ----------

#Read file as a stream and write to Delta
#Using 64MB as the default buffersize here
df = spark.readStream.option("buffersize", 67108864).format("payer-mrf").load(source_data)
query = (
df.writeStream 
 .outputMode("append") 
 .format("delta")
 .trigger(availableNow=True)
 .option("truncate", "false") 
 .option("checkpointLocation", checkpoint_path)
 .table(ingest_table)
)

# COMMAND ----------

query.awaitTermination()
print("Query finished")

# COMMAND ----------

# MAGIC %md
# MAGIC Create tables from JSON structures for ETL development in SQL

# COMMAND ----------

# Mapping to RDDs where json schema can be inferred when creating a dataframe
# Schemas will be distinct between
# 1. in_network array
# 2. provider_references array 
# 3. Any other header information

provider_references_rdd = spark.sql(f"select json_payload from {ingest_table} where header_key='provider_references'").rdd.flatMap(lambda x:x)

header_rdd = spark.sql(f"select json_payload from {ingest_table} where header_key=''").rdd.flatMap(lambda x:x)

in_network_rdd = spark.sql(f"select json_payload from {ingest_table} where header_key='in_network'").rdd.flatMap(lambda x:x)

# COMMAND ----------

# Creating Dataframes from the 3 distinct schemas above and saving to a table
spark.read.json(header_rdd).write.mode("overwrite").saveAsTable(provider_header_table)
spark.read.json(provider_references_rdd).write.mode("overwrite").saveAsTable(provider_references_table)
spark.read.json(in_network_rdd).write.mode("overwrite").saveAsTable(in_network_table)

# COMMAND ----------

# MAGIC %md ### Silver (Create relational tables from nested array structures)
# MAGIC ETL Curation to report off of 2023 mandate. Compare prices for a procedure (BILLING_CODE) within a provider group (TIN)

# COMMAND ----------

# MAGIC %md
# MAGIC ![logo](https://github.com/databricks-industry-solutions/hls-payer-mrf-sparkstreaming/blob/main/img/silver.jpg?raw=True)

# COMMAND ----------

spark.sql(f"""
CREATE TABLE {provider_x_payer_table}
AS
SELECT reporting_entity_name, reporting_entity_type, foo.provider_group_id, foo.group_array.npi, foo.group_array.tin
FROM (
  SELECT provider_group_id, explode(provider_groups) AS group_array
  FROM {provider_references_table}
) foo
INNER JOIN (
  SELECT reporting_entity_name, reporting_entity_type
  FROM {provider_header_table}
  WHERE reporting_entity_name IS NOT NULL
) entity
ON 1=1
""")

spark.sql(f"""
CREATE TABLE {codes_table}
AS
SELECT uuid() AS sk_in_network_id
  ,n.billing_code
  ,n.billing_code_type
  ,n.billing_code_type_version
  ,n.description
  ,n.name
  ,n.negotiation_arrangement
  ,n.negotiated_rates
FROM {in_network_table} n
""")

spark.sql(f"""
CREATE TABLE {rates_table}
AS
SELECT uuid() AS sk_rate_id
  ,foo.sk_in_network_id
  ,foo.negotiated_rates_array
FROM (
  SELECT sk_in_network_id, explode(c.negotiated_rates) AS negotiated_rates_array
  FROM {codes_table} c
) foo
""")

spark.sql(f"""
CREATE TABLE {prices_table}
AS
SELECT sk_in_network_id, sk_rate_id, price.billing_class, price.billing_code_modifier, price.expiration_date, price.negotiated_rate, price.negotiated_type, price.service_code
FROM (
  SELECT explode(negotiated_rates_array.negotiated_prices) AS price, sk_rate_id, sk_in_network_id
  FROM {rates_table}
) foo
WHERE price.negotiated_type = 'negotiated'
""")

spark.sql(f"""
CREATE TABLE {par_providers_table}
AS
SELECT provider_reference_id, sk_rate_id
FROM (
  SELECT explode(negotiated_rates_array.provider_references) AS provider_reference_id, sk_rate_id
  FROM {rates_table}
) foo
""")

# COMMAND ----------

# MAGIC %md
# MAGIC ### Gold (Sample search query for price comparison)
# MAGIC 2023/2024 Shoppable prices using some random examples of billing code and provider practice

# COMMAND ----------

dbutils.widgets.text("billing_code", "43283")
dbutils.widgets.text("tin_value", "161294447")

billing_code = dbutils.widgets.get("billing_code").replace("'", "''")
tin_value = dbutils.widgets.get("tin_value").replace("'", "''")

price_comparison_df = spark.sql(f"""
SELECT billing_code, description, billing_class, billing_code_modifier, service_code, negotiated_rate, npi, tin
FROM {codes_table} proc
INNER JOIN {prices_table} price
  ON proc.sk_in_network_id = price.sk_in_network_id
INNER JOIN {par_providers_table} provider_ref
  ON price.sk_rate_id = provider_ref.sk_rate_id
INNER JOIN {provider_x_payer_table} provider
  ON provider_ref.provider_reference_id = provider.provider_group_id
WHERE billing_code = '{billing_code}'
  AND negotiation_arrangement = 'ffs'
  AND tin.value = '{tin_value}'
""")

display(price_comparison_df)
