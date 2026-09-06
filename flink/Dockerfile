# Flink with PyFlink. The official flink images ship no Python, and there is no official
# PyFlink image, so this adds the runtime the job needs.
#
# Python lives here rather than in the host venv on purpose: PyFlink publishes no wheel for
# Windows on Python 3.12, so a host install would mean building from source against a
# toolchain this machine does not have. Running Flink as a cluster is also what
# ARCHITECTURE.md section 9 describes, so this is the shape the design already called for
# rather than a workaround.
FROM flink:1.20.1-scala_2.12-java17

USER root

RUN apt-get update -y \
 && apt-get install -y --no-install-recommends python3 python3-pip python3-dev \
 && ln -sf /usr/bin/python3 /usr/bin/python \
 && rm -rf /var/lib/apt/lists/*

# Pinned to the cluster's own version: PyFlink bundles a matching JAR set, and a mismatch
# surfaces as a confusing serialization error at job submission rather than at install.
RUN pip3 install --no-cache-dir apache-flink==1.20.1

# The Kafka connector is not in the base image. The SQL-connector fat JAR is used because it
# bundles the Kafka client, so there is no separate client JAR to keep in step.
ARG FLINK_KAFKA_CONNECTOR=3.3.0-1.20
RUN curl -fsSL -o /opt/flink/lib/flink-sql-connector-kafka.jar \
    "https://repo1.maven.org/maven2/org/apache/flink/flink-sql-connector-kafka/${FLINK_KAFKA_CONNECTOR}/flink-sql-connector-kafka-${FLINK_KAFKA_CONNECTOR}.jar"

RUN mkdir -p /opt/vigil /flink-checkpoints /flink-savepoints \
 && chown -R flink:flink /opt/vigil /flink-checkpoints /flink-savepoints

USER flink
