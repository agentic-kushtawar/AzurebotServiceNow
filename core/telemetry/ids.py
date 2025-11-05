from opentelemetry import trace

def current_run_id() -> str:
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if not ctx or not ctx.is_valid:
        return "-"
    return format(ctx.trace_id, "032x")[:8]   # short 8-char run-id
