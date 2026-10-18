# sentinelops — Kubernetes Anomaly Detector + Slack Alerts

## What this is

sentinelops watches Kubernetes events and container logs, detects known-bad
patterns (CrashLoopBackOff, OOMKilled, ImagePullBackOff, FailedScheduling,
probe failures, evictions) plus sudden error-rate spikes in logs, builds an
incident summary, and posts it to Slack.

- **Detection** works fully offline on an events JSONL file (`sample_data/`
  included) or live via `kubectl get events -A -o json` with `--live`.
- **Summaries** default to a local heuristic summary; `--mode ai` uses
  Anthropic Claude for a richer root-cause write-up (needs your own
  `ANTHROPIC_API_KEY`).
- **Slack** defaults to a dry run that prints the exact payload it *would*
  send; set `SLACK_WEBHOOK_URL` and pass `--notify` to post for real.

## What problem it solves

In a busy cluster, the first sign of trouble is buried in `kubectl get
events` noise or a wall of log lines. Crash loops, OOM kills and scheduling
failures often sit unnoticed until a customer reports an outage. Teams react
to incidents instead of catching them in the first minutes.

## Benefits

- **Minutes, not hours:** anomalies are grouped into incidents with severity,
  affected pods and a recommended first action — the on-call engineer starts
  from a diagnosis, not a blank `kubectl` prompt.
- **Proactive alerting:** error-rate spikes are detected from container logs
  even before K8s events flag a pod, catching degrading services early.
- **No cluster needed to evaluate:** the bundled synthetic sample data runs
  the full pipeline (detect → summarize → Slack payload) on any machine.
- **Safe by default:** Slack posting is dry-run unless you explicitly opt in,
  so there is no risk of spamming a channel while experimenting.

## Before vs After

| Before | After |
|---|---|
| `kubectl get events` scrolled manually; CrashLoopBackOff found 30 min after it started | `sentinelops.py detect` → `[CRITICAL] CrashLoopBackOff x4` with pod + fix in seconds |
| Error-rate spike noticed only when dashboards are checked | Log spike detection flags the 5-minute window automatically |
| On-call writes the incident summary from scratch in Slack | Summary + recommended actions generated; dry-run payload reviewed, one flag to post |

## How to run the demo

```bash
chmod +x demo.sh
./demo.sh
```

Expected output: 5 detected incidents (CrashLoopBackOff, OOMKilled,
ImagePullBackOff, FailedScheduling, one merged error-rate spike window)
followed by the dry-run Slack payload. No cluster, keys or network needed.

Live mode against a real cluster:

```bash
python3 sentinelops.py detect --live
python3 sentinelops.py detect --live --notify   # needs SLACK_WEBHOOK_URL
```

AI summary mode (needs your own key):

```bash
cp .env.example .env        # add ANTHROPIC_API_KEY (and SLACK_WEBHOOK_URL)
pip install -r requirements.txt
python3 sentinelops.py detect sample_data/events.jsonl --mode ai
```

## Architecture (short)

```
events.jsonl  ──► parse_event() ──► detect_event_anomalies()
                                      (regex rule table: reason+message)
app.log       ──► detect_log_spikes()
                      (per-minute ERROR buckets, lower-quartile baseline,
                       3x spike threshold, consecutive windows merged)
      │
      ▼
heuristic_summary()  or  ai_summary() (Anthropic, opt-in)
      │
      ▼
slack_payload() ──► dry-run print  /  POST to SLACK_WEBHOOK_URL (--notify)
```

## Roadmap

- Watch mode: poll `kubectl get events` on an interval instead of one-shot.
- Prometheus alertmanager webhook receiver as an additional input.
- Deduplication / silencing windows so flapping alerts do not spam Slack.
- Severity auto-escalation (page after N repeats in M minutes).

## Notes

- All data in `sample_data/` is synthetic, written for the demo.
- Heuristic mode never sends your events or logs anywhere.
