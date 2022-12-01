package com.databricks.labs.sparkstreaming.jsonmrf


import com.google.common.io.ByteStreams
import org.apache.hadoop.conf.Configuration
import org.apache.spark.unsafe.types.UTF8String
import org.apache.spark.{InterruptibleIterator, Partition, SparkContext, TaskContext}
import org.apache.spark.rdd.RDD
import org.apache.spark.sql.catalyst.InternalRow
import org.apache.hadoop.fs.{FileSystem, Path, FSDataInputStream}
import java.io.BufferedInputStream
import scala.util.Random
import org.apache.spark.SerializableWritable
import org.apache.spark.broadcast.Broadcast

case class JsonPartition(start: Long, end: Long, idx: Int) extends Partition{
  override def index: Int = idx
}

/*
 * Represents an offset for Spark to consume. This can be made up of one or more Byte Arrays
 */
private class JsonMRFRDD(
  sc: SparkContext,
  confBroadcast: Broadcast[SerializableWritable[Configuration]],
  partitions: Array[JsonPartition],
  fileName: Path)
    extends RDD[InternalRow](sc, Nil) {

  override def getPartitions: Array[Partition] = {
   partitions.indices.map { i =>
      new JsonPartition(partitions(i).start, partitions(i).end, i).asInstanceOf[Partition]
    }.toArray
   }

  //Only ever returning one "row" with the iterator...
  //Maybe change this in the future to break apart the json object further into individual rows?
  override def compute(thePart: Partition, context: TaskContext): Iterator[InternalRow] =  {
    val in = JsonMRFRDD.fs.open(fileName)
    println("Starting computation on fileOffset: " + thePart.asInstanceOf[JsonPartition].start + " : and current position in the inputstream" + in.getPos)
    //Close out fis, bufferinputstream objects, etc
    val part = thePart.asInstanceOf[JsonPartition]
    in.seek(part.start)
    println("the updated position offset after seeking" + in.getPos + "\nstarting the byte consumption")

    val buffer = new Array[Byte](( part.end - part.start + 1).toInt)
    ByteStreams.readFully(in, buffer)
    in.close
    println("Finished computing fileOffset: " + thePart.asInstanceOf[JsonPartition].start)
    Seq(InternalRow(UTF8String.fromBytes(buffer))).toIterator
  }
}


object JsonMRFRDD{
  val fs = FileSystem.get(new Configuration)
}
