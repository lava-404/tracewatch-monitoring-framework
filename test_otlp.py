import urllib.request
import urllib.error

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.trace.v1.trace_pb2 import Span
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue

request = ExportTraceServiceRequest()

span = Span()
span.trace_id = bytes.fromhex("1234567890abcdef1234567890abcdef")
span.span_id = bytes.fromhex("1234567890abcdef")
span.name = "direct-indexer-test"
span.kind = Span.SPAN_KIND_INTERNAL
span.start_time_unix_nano = 1730000000000000000
span.end_time_unix_nano = 1730000001000000000

resource_spans = request.resource_spans.add()
resource_spans.resource.attributes.append(
    KeyValue(key="service.name", value=AnyValue(string_value="direct-test"))
)

scope_spans = resource_spans.scope_spans.add()
scope_spans.spans.append(span)

payload = request.SerializeToString()

req = urllib.request.Request(
    "http://localhost:8005/v1/traces",
    data=payload,
    headers={"Content-Type": "application/x-protobuf"},
    method="POST",
)

try:
    with urllib.request.urlopen(req, timeout=10) as response:
        print("HTTP Status:", response.status)
        print("Response:", response.read().decode())
except urllib.error.HTTPError as e:
    print("HTTP Status:", e.code)
    print("Response:", e.read().decode())
except Exception as e:
    print("Error:", repr(e))
