# Webhook delivery runbook

## Verify a signature

Verify the HMAC-SHA256 signature against the raw request body before parsing JSON. Read the signature from the X-Relay-Signature header. Reject requests whose timestamp is more than five minutes old.

## Delivery retries

RelayOps retries webhook deliveries after network errors and HTTP 429 or 5xx responses. Retries stop 24 hours after the initial delivery. Any HTTP 2xx response acknowledges delivery.

## Duplicate events

Every retry reuses the original event ID. Store the event ID in a database column with a unique constraint so duplicate deliveries do not repeat side effects. Return HTTP 200 when the event has already been processed.

## Rotate a webhook secret

When a webhook secret is rotated, the previous secret remains valid for 15 minutes. Update the consumer to verify both secrets during that window, then remove the previous secret.
