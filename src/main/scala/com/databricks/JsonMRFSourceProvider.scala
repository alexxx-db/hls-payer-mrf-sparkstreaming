package com.databricks.labs.sparkstreaming.jsonmrf

import com.google.common.io.ByteStreams
import org.apache.hadoop.fs.{FileSystem, Path}
import org.apache.spark.sql.execution.streaming.Source
import org.apache.spark.sql.sources.{DataSourceRegister, StreamSourceProvider}
import org.apache.spark.sql.types._
import org.apache.spark.sql.SQLContext

import java.io.{BufferedInputStream, BufferedOutputStream}
import java.net.URI
import java.util.zip.GZIPInputStream

class JsonMRFSourceProvider extends StreamSourceProvider with DataSourceRegister {

  override def shortName(): String = "payer-mrf"

  override def sourceSchema(sqlContext: SQLContext,
    schema: Option[StructType],
    providerName: String,
    parameters: Map[String, String]): (String, StructType) = {
    (shortName(), JsonMRFSource.getSchema(
      parameters.get("payloadAsArray") match {
        case Some("true") => true
        case _ => false
      },
      parameters.get("includeOffsets") match {
        case Some("true") => true
        case _ => false
      }
    ))
  }

  override def createSource(sqlContext: SQLContext,
    metadataPath: String,
    schema: Option[StructType],
    providerName: String,
    parameters: Map[String, String]): Source = {

    val filesystem_param = parameters.getOrElse("filesystem", parameters.get("path").get.split(":/")(0))
    val params = parameters.get("path").get match {

      case ext if ext.endsWith(".gz") =>
        val fs = FileSystem.get(URI.create(ext), sqlContext.sparkSession.sparkContext.hadoopConfiguration)
        val inStream = new BufferedInputStream(new GZIPInputStream(fs.open(new Path(ext))), 268435456) //256MB
        val fileName = if (ext.dropRight(3).endsWith(".json")) ext.dropRight(3) else ext.dropRight(3)+".json"
        val outStream = new BufferedOutputStream(fs.create(new Path(fileName) ,true))
        ByteStreams.copy(inStream, outStream)
        inStream.close
        outStream.close
        parameters  + ("uncompressedPath" -> ext.dropRight(3)) + ("filesystem" -> filesystem_param)

      case ext if ext.endsWith(".json") =>
        parameters + ("uncompressedPath" -> ext) + ("filesystem" -> filesystem_param)

      case _ => throw new Exception("codec for file extension not implemented yet")
    }
    new JsonMRFSource(sqlContext, params)
  }
}
