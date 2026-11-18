"""Cloud Monitoring metrics from the services' structured logs. Traces and logs need no setup of their own: the services
export spans to Cloud Trace themselves (shared/telemetry.py), and Cloud Logging collects their stdout."""

from googleapiclient.errors import HttpError

from infra.gcp import client, execute, ignore_missing, project_id

# Answers the questions a trace cannot: how the traffic splits across model tiers, and how often tools fail or the
# policy says no (outcome="POLICY_DENIED"). Each is a count of one log line, with some of that line's fields as labels.
LOG_METRICS = {
    "sap_agent/tool_calls": {
        "description": "Tool calls handled by the tool server, by tool and outcome (OK or an error code).",
        "filter": 'jsonPayload.message="tool_call"',
        "labels": {"tool": "jsonPayload.tool", "outcome": "jsonPayload.outcome"},
    },
    "sap_agent/model_routes": {
        "description": "Customer messages routed to each model tier.",
        "filter": 'jsonPayload.message="model_route"',
        "labels": {"tier": "jsonPayload.tier"},
    },
}
_ALREADY_EXISTS = 409


def enable_observability() -> None:
    metrics = client("logging", "v2").projects().metrics()
    for name, spec in LOG_METRICS.items():
        body = {
            "name": name,
            "description": spec["description"],
            "filter": spec["filter"],
            "metricDescriptor": {
                "metricKind": "DELTA",
                "valueType": "INT64",
                "labels": [{"key": label, "valueType": "STRING"} for label in spec["labels"]],
            },
            "labelExtractors": {label: f"EXTRACT({field})" for label, field in spec["labels"].items()},
        }
        try:
            execute(metrics.create(parent=f"projects/{project_id()}", body=body))
        except HttpError as error:
            if error.resp.status != _ALREADY_EXISTS:
                raise
            execute(metrics.update(metricName=f"projects/{project_id()}/metrics/{name}", body=body))


def destroy_observability() -> None:
    """Remove the metrics; logs and traces stay until their retention ends, so they remain available as evidence."""
    metrics = client("logging", "v2").projects().metrics()
    for name in LOG_METRICS:
        ignore_missing(metrics.delete(metricName=f"projects/{project_id()}/metrics/{name}"))
