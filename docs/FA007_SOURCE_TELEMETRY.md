# FA-007 source-linked status projection (partial)

A pure, secret-free projection records exact repository and commit identity,
event kind, observation timestamp and freshness. A stale successful observation
is downgraded to unknown; future timestamps, missing SHAs, invalid statuses
and unbounded freshness windows are rejected.

This does not prove the observation was collected by a trusted source, or
provide a dashboard/API/alert endpoint. Integrate with authenticated health,
crawler run receipts and live incident correlation only after source evidence
is verified. Do not transmit personal/source contents or secret values in
telemetry. Unit test: python -m unittest tests.test_telemetry.
