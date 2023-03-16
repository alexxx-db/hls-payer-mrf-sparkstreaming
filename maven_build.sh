#To import:
#repo: https://github.com/databricks-industry-solutions/hls-payer-mrf-sparkstreaming/raw/maven
#coordinates: databricks-industry-solutions:hls-payer-mrf-sparkstreaming:0.3.5
#
#to run install maven, build the jar using sbt package, and use ./maven_build.sh


mvn install:install-file -DgroupId=databricks-industry-solutions -DartifactId=hls-payer-mrf-sparkstreaming -Dversion=0.3.5 -Dfile=target/scala-2.12/payer-mrf-streamsource-0.3.4.jar -Dpackaging=jar -DgeneratePom=true -DlocalRepositoryPath=.  -DcreateChecksum=true