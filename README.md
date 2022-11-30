# SparkStreamSources
Spark Custom Stream Source and Sink for Payer MRF Use Case

## Recommended Spark Settings

``` python
#Spark Settings
spark.rpc.message.maxSize 1024
spark.driver.memory 12g
spark.driver.cores 2

#JVM Settings (8g or higher)
JAVA_OPTS=-Xmx8g -Xms8g

```

## Running

``` python
df = spark.readStream
    .format("com.databricks.labs.sparkstreaming.jsonmrf.JsonMRFSourceProvider")
    .load("/Users/aaron.zavora//Downloads/umr-tpa-encore-in-network-rates.json")

df.writeStream
    .outputMode("append")
    .format("text")
    .queryName("umr-tpa-in-network-parsing")
    .option("checkpointLocation", "src/test/resources/chkpoint_dir")
    .start("src/test/resources/output")
``` 

## Use Case 

Schema definition that is parsed is the CMS in-network file. https://github.com/CMSgov/price-transparency-guide/tree/master/schemas/in-network-rates

## Unzipping Recommended First due to compression level which may cause performance issues in buffering...


```python
#3.6G zipped, 120G unzipped file 
#download to local storage "Command took 17.75 minutes"
wget -O ./2022-08-01_umr_inc_tpa_encore_non_evaluated_gap_enc-in-network-rates.json.gz https://uhc-tic-mrf.azureedge.net/public-mrf/2022-08-01/2022-08-01_UMR--Inc-_TPA_ENCORE-ENTERPRISES-AIRROSTI-DCI_TX-DALLAS-NON-EVALUATED-GAP_-ENC_NXBJ_in-network-rates.json.gz

#unzip to DBFS  "Command took 26.83 minutes"
gunzip -cd ./2022-08-01_umr_inc_tpa_encore_non_evaluated_gap_enc-in-network-rates.json.gz > /dbfs/user/hive/warehouse/hls_dev_payer_transparency.db/raw_files/2022-08-01_umr_inc_tpa_encore_non_evaluated_gap_enc-in-network-rates.json 
```


## Data Output

``` bash
more  src/test/resources/output/part-00000-a6af8cf3-6162-4d60-9acb-8933bac19b8b-c000.txt
>[{"negotiation_arrangement":"ffs","name":"BRONCHOSCOPY W/TRANSBRONCHIAL LUNG BX EACH LOBE","billi
...
>[{"negotiation_arrangement":"ffs","name":"ANESTHESIA EXTENSIVE SPINE & SPINAL CORD","bil

```

## Speed 

On a local Macbook with xmx8g running at 2.5GB per minute. Note of caution, this program depends on buffering. Some forms of .gz extension do not enable efficient buffering in the JVM. It is recommended to gunzip -d the file first prior to running

This project serves as an example to implement Apache Spark custom Structured Streaming Sources. 

This project is accompanied by [Spark Custom Stream Sources](https://hackernoon.com/spark-custom-stream-sources-ec360b8ae240)


