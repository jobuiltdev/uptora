# Private-alpha data retention

The alpha begins with documented retention rather than automatic deletion. The
operator reviews volume weekly; cleanup is introduced only when storage growth
requires it and must be bounded, idempotent, observable, and tested.

| Data | Alpha policy |
| --- | --- |
| CheckResults | Retain 90 days. Never delete results needed by an open incident. |
| Screenshot evidence | Retain 30 days after capture, but retain evidence referenced by an available IncidentShare until that share expires or is revoked. |
| Resolved incidents | Retain at least 12 months and for the duration of the alpha. |
| Notification events/deliveries | Retain 180 days; failed deliveries remain available for operator review. |
| MonitorRuns | Retain completed/cancelled runs 90 days and internally failed runs 180 days. |
| IncidentShare snapshots | Retain while the incident exists; revoked/expired share rows remain for at least 90 days for audit. |

IncidentShare's bounded JSON snapshot preserves factual incident proof if old
CheckResults are later removed. Screenshot bytes are not duplicated: removing
the underlying file makes shared evidence unavailable while the factual report
continues to work.

Before introducing cleanup, test these invariants:

1. batches have an explicit maximum size;
2. rerunning the job changes nothing after the first successful pass;
3. open incidents and available share evidence are excluded;
4. storage deletion happens only after the database selection is committed or
   is safely retryable;
5. each run logs counts and cutoffs, never tokens, field values, or paths.
