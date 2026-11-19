import asyncio
import logging
import os
import json
import gzip

from contextlib import asynccontextmanager
from datetime import datetime, timezone

import asyncpg

from fastapi import FastAPI, Request, HTTPException

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("batch-indexer")


DATABASE_URL = os.getenv("SUPABASE_DB_URL")
BATCH_SIZE = int(os.getenv("INDEXER_BATCH_SIZE", "500"))
FLUSH_INTERVAL = int(os.getenv("INDEXER_FLUSH_INTERVAL", "5"))

buffer = []
buffer_lock = asyncio.Lock()
db_pool = None


def decode_attributes(attributes):
    result = {}

    for attribute in attributes:
        value = attribute.value

        if value.string_value:
            result[attribute.key] = value.string_value
        elif value.bool_value:
            result[attribute.key] = value.bool_value
        elif value.int_value:
            result[attribute.key] = value.int_value
        elif value.double_value:
            result[attribute.key] = value.double_value
        elif value.array_value.values:
            result[attribute.key] = [
                v.string_value or v.int_value or v.double_value
                for v in value.array_value.values
            ]
        elif value.kvlist_value.values:
            result[attribute.key] = decode_attributes(
                value.kvlist_value.values
            )

    return result


def decode_span(span, resource_attributes, service_name):
    start_time = datetime.fromtimestamp(
        span.start_time_unix_nano / 1_000_000_000,
        tz=timezone.utc,
    )

    end_time = datetime.fromtimestamp(
        span.end_time_unix_nano / 1_000_000_000,
        tz=timezone.utc,
    )

    duration_ms = (
        span.end_time_unix_nano - span.start_time_unix_nano
    ) / 1_000_000

    status = "ERROR" if span.status.code == 2 else "OK"

    return {
        "trace_id": span.trace_id.hex(),
        "span_id": span.span_id.hex(),
        "parent_span_id": span.parent_span_id.hex() or None,
        "service_name": service_name,
        "span_name": span.name,
        "span_kind": str(span.kind),
        "start_time": start_time,
        "end_time": end_time,
        "duration_ms": duration_ms,
        "status": status,
        "status_message": span.status.message,
        "attributes": decode_attributes(span.attributes),
        "events": [
            {
                "name": event.name,
                "timestamp": datetime.fromtimestamp(
                    event.time_unix_nano / 1_000_000_000,
                    tz=timezone.utc,
                ).isoformat(),
                "attributes": decode_attributes(event.attributes),
            }
            for event in span.events
        ],
        "links": [
            {
                "trace_id": link.trace_id.hex(),
                "span_id": link.span_id.hex(),
                "attributes": decode_attributes(link.attributes),
            }
            for link in span.links
        ],
        "resource_attributes": resource_attributes,
    }


async def flush_batch():
    global buffer

    async with buffer_lock:
        if not buffer:
            return

        batch = buffer[:]
        buffer = []

    try:
        await persist_batch(batch)
        logger.info("Indexed %s spans successfully", len(batch))

    except Exception as exc:
        logger.exception("Batch indexing failed")

        # Restore failed batch to buffer
        async with buffer_lock:
            buffer = batch + buffer

        raise exc


async def persist_batch(spans):
    async with db_pool.acquire() as conn:
        async with conn.transaction():

            batch_record = await conn.fetchrow(
                """
                INSERT INTO observability.ingestion_batches
                    (trace_count, span_count, status)
                VALUES (0, $1, 'processing')
                RETURNING id
                """,
                len(spans),
            )

            batch_id = batch_record["id"]

            traces = {}

            for span in spans:
                trace_id = span["trace_id"]

                if trace_id not in traces:
                    traces[trace_id] = []

                traces[trace_id].append(span)

            for trace_id, trace_spans in traces.items():

                root_span = next(
                    (
                        span
                        for span in trace_spans
                        if not span["parent_span_id"]
                    ),
                    trace_spans[0],
                )

                start_time = min(
                    span["start_time"] for span in trace_spans
                )

                end_time = max(
                    span["end_time"] for span in trace_spans
                )

                duration_ms = (
                    end_time - start_time
                ).total_seconds() * 1000

                services = list(
                    set(
                        span["service_name"]
                        for span in trace_spans
                    )
                )

                error_count = sum(
                    1
                    for span in trace_spans
                    if span["status"] == "ERROR"
                )

                trace_status = (
                    "ERROR" if error_count > 0 else "OK"
                )

                await conn.execute(
                    """
                    INSERT INTO observability.trace_index (
                        trace_id,
                        root_service_name,
                        root_span_name,
                        start_time,
                        end_time,
                        duration_ms,
                        span_count,
                        status,
                        error_count,
                        services,
                        attributes
                    )
                    VALUES (
                        $1, $2, $3, $4, $5, $6,
                        $7, $8, $9, $10, $11
                    )
                    ON CONFLICT (trace_id)
                    DO UPDATE SET
                        end_time = EXCLUDED.end_time,
                        duration_ms = EXCLUDED.duration_ms,
                        span_count = EXCLUDED.span_count,
                        status = EXCLUDED.status,
                        error_count = EXCLUDED.error_count,
                        services = EXCLUDED.services
                    """,
                    trace_id,
                    root_span["service_name"],
                    root_span["span_name"],
                    start_time,
                    end_time,
                    duration_ms,
                    len(trace_spans),
                    trace_status,
                    error_count,
                    services,
                    json.dumps(root_span["attributes"]),
                )

                for span in trace_spans:

                    await conn.execute(
                        """
                        INSERT INTO observability.span_index (
                            span_id,
                            trace_id,
                            parent_span_id,
                            service_name,
                            span_name,
                            span_kind,
                            start_time,
                            end_time,
                            duration_ms,
                            status,
                            status_message,
                            attributes,
                            events,
                            links,
                            resource_attributes
                        )
                        VALUES (
                            $1, $2, $3, $4, $5,
                            $6, $7, $8, $9, $10,
                            $11, $12, $13, $14, $15
                        )
                        ON CONFLICT (span_id)
                        DO UPDATE SET
                            status = EXCLUDED.status,
                            end_time = EXCLUDED.end_time,
                            duration_ms = EXCLUDED.duration_ms,
                            attributes = EXCLUDED.attributes
                        """,
                        span["span_id"],
                        span["trace_id"],
                        span["parent_span_id"],
                        span["service_name"],
                        span["span_name"],
                        span["span_kind"],
                        span["start_time"],
                        span["end_time"],
                        span["duration_ms"],
                        span["status"],
                        span["status_message"],
                        json.dumps(span["attributes"]),
                        json.dumps(span["events"]),
                        json.dumps(span["links"]),
                        json.dumps(span["resource_attributes"]),
                    )

            await conn.execute(
                """
                UPDATE observability.ingestion_batches
                SET
                    trace_count = $1,
                    span_count = $2,
                    processed_at = NOW(),
                    status = 'completed'
                WHERE id = $3
                """,
                len(traces),
                len(spans),
                batch_id,
            )


async def background_flush():
    while True:
        await asyncio.sleep(FLUSH_INTERVAL)

        try:
            await flush_batch()
        except Exception:
            logger.exception("Periodic flush failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global db_pool

    if not DATABASE_URL:
        raise RuntimeError("SUPABASE_DB_URL is not configured")

    db_pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=2,
        max_size=10,
        ssl="require",
    )

    flush_task = asyncio.create_task(background_flush())

    logger.info("Batch indexer started")

    yield

    flush_task.cancel()

    try:
        await flush_task
    except asyncio.CancelledError:
        pass

    try:
        await flush_batch()
    except Exception:
        logger.exception("Final flush failed")

    await db_pool.close()


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "buffered_spans": len(buffer),
    }


@app.post("/v1/traces")
async def receive_traces(request: Request):
    global buffer

    try:
        body = await request.body()

        content_encoding = request.headers.get(
            "content-encoding", ""
        ).lower()

        logger.info(
            "OTLP request: content-type=%s "
            "content-encoding=%s body-size=%d",
            request.headers.get("content-type"),
            content_encoding,
            len(body),
        )

        # FIX: OTel Collector sends gzip-compressed protobuf
        if content_encoding == "gzip":
            body = gzip.decompress(body)

        otlp_request = ExportTraceServiceRequest()
        otlp_request.ParseFromString(body)

        incoming_spans = []

        for resource_spans in otlp_request.resource_spans:

            resource_attributes = decode_attributes(
                resource_spans.resource.attributes
            )

            service_name = resource_attributes.get(
                "service.name",
                "unknown-service",
            )

            for scope_spans in resource_spans.scope_spans:

                for span in scope_spans.spans:

                    incoming_spans.append(
                        decode_span(
                            span,
                            resource_attributes,
                            service_name,
                        )
                    )

        async with buffer_lock:
            buffer.extend(incoming_spans)
            current_size = len(buffer)

        logger.info(
            "Received %s spans | buffer size: %s",
            len(incoming_spans),
            current_size,
        )

        if current_size >= BATCH_SIZE:
            await flush_batch()

        return {"accepted": len(incoming_spans)}

    except Exception as exc:
        logger.exception("Failed to process OTLP request")

        raise HTTPException(
            status_code=500,
            detail=str(exc),
        )