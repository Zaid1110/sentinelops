#!/usr/bin/env python3
"""
sentinelops: Kubernetes anomaly detector + Slack incident alerts.

Reads Kubernetes events (JSONL sample file, or live `kubectl get events`),
detects known bad patterns (CrashLoopBackOff, OOMKilled, ImagePullBackOff,
FailedScheduling) plus error-rate spikes in container logs, builds an incident
summary, and posts it to Slack.

Summary modes:
  * heuristic (default) - local rule-based summary, no API keys.
  * ai            - richer root-cause summary via Anthropic Claude
                    (needs ANTHROPIC_API_KEY in env).

Slack posting:
  * default is DRY RUN - prints the payload that WOULD be sent.
  * set SLACK_WEBHOOK_URL and pass --notify to actually post.

Usage:
    python3 sentinelops.py detect sample_data/events.jsonl
    python3 sentinelops.py detect sample_data/events.jsonl --logs sample_data/app.log
    python3 sentinelops.py detect --live            # needs kubectl
    python3 sentinelops.py detect sample_data/events.jsonl --notify   # real Slack post
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone

# ------------------------------------------------------------ detection

# (name, severity, event reason/message match, recommended action)
ANOMALY_RULES: list[tuple[str, str, "re.Pattern[str]", str]] = [
    (
        "CrashLoopBackOff",
        "critical",
        re.compile(r"CrashLoopBackOff|Back-off restarting failed container"),
        "Check `kubectl logs <pod> --previous` for the crash reason, fix the "
        "code/config, and verify liveness/readiness probes are not killing a "
        "slow-starting container.",
    ),
    (
        "OOMKilled",
        "critical",
        re.compile(r"OOMKilled|OOMKilling|Memory cgroup out of memory"),
        "Raise the container memory limit (or fix the memory leak), then "
        "re-deploy. Check for unbounded caches / connection pools.",
    ),
    (
        "ImagePullBackOff",
        "high",
        re.compile(r"ImagePullBackOff|Failed to pull image"),
        "Verify the image tag exists in the registry and the imagePullSecret "
        "is valid; fix the tag or registry credentials.",
    ),
    (
        "FailedScheduling",
        "high",
        re.compile(r"FailedScheduling|Insufficient (cpu|memory)"),
        "The cluster has no room for the pod. Scale the node pool (cluster "
        "autoscaler), lower the pod requests, or free capacity.",
    ),
    (
        "Probe failures",
        "medium",
        re.compile(r"Liveness probe failed|Readiness probe failed|Startup probe failed"),
        "The container is up but not healthy. Check the probe endpoint/path, "
        "increase timeouts, or fix the app health check.",
    ),
    (
        "Evicted",
        "high",
        re.compile(r"\bEvicted\b|The node was low on resource"),
        "Pod was evicted under node pressure. Set proper requests/limits and "
        "consider PodDisruptionBudgets for critical workloads.",
    ),
]


def parse_event(obj: dict) -> dict:
    io = obj.get("involvedObject", {}) or {}
    return {
        "time": obj.get("lastTimestamp") or obj.get("firstTimestamp") or "",
        "type": obj.get("type", ""),
        "reason": obj.get("reason", ""),
        "message": obj.get("message", ""),
        "pod": io.get("name", ""),
        "namespace": io.get("namespace", ""),
        "kind": io.get("kind", ""),
    }


def load_events(path: str | None, live: bool) -> list[dict]:
    if live:
        result = subprocess.run(
            ["kubectl", "get", "events", "-A", "-o", "json"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"kubectl failed: {result.stderr.strip()}", file=sys.stderr)
            sys.exit(2)
        data = json.loads(result.stdout)
        return [parse_event(item) for item in data.get("items", [])]
    events: list[dict] = []
    with open(path, "r", encoding="utf-8") as fh:  # type: ignore[arg-type]
        for line in fh:
            line = line.strip()
            if line:
                events.append(parse_event(json.loads(line)))
    return events


def detect_event_anomalies(events: list[dict]) -> list[dict]:
    counts: dict[tuple[str, str, str], int] = defaultdict(int)
    for ev in events:
        blob = f"{ev['reason']} {ev['message']}"
        for name, severity, pattern, action in ANOMALY_RULES:
            if pattern.search(blob):
                counts[(name, severity, action)] += 1

    incidents: list[dict] = []
    for (name, severity, action), count in counts.items():
        # Thresholds: crash loops and scheduling failures need repeats to
        # count as an incident; OOM/image-pull fire on the first hit.
        threshold = 2 if name in ("CrashLoopBackOff", "FailedScheduling") else 1
        if count >= threshold:
            affected = sorted(
                {f"{ev['namespace']}/{ev['pod']}" for ev in events
                 if any(r[0] == name and r[2].search(f"{ev['reason']} {ev['message']}")
                        for r in ANOMALY_RULES)}
            )
            incidents.append(
                {
                    "name": name,
                    "severity": severity,
                    "occurrences": count,
                    "affected": affected,
                    "recommended_action": action,
                }
            )
    return incidents


ERROR_RE = re.compile(r"\b(ERROR|Error|error|FATAL|panic|Traceback)\b")


def detect_log_spikes(path: str, bucket_minutes: int = 5) -> list[dict]:
    """Bucket ERROR lines per time window; flag a bucket >3x the baseline."""
    buckets: dict[str, int] = defaultdict(int)
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not ERROR_RE.search(line):
                continue
            m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})", line)
            if not m:
                continue
            buckets[m.group(1)] += 1
    if len(buckets) < 3:
        return []
    counts = sorted(buckets.values())
    # Lower quartile as the baseline, so a sustained spike does not
    # inflate the baseline the way a plain median would.
    baseline = counts[len(counts) // 4] or 1
    threshold = max(3 * baseline, 5)
    spike_buckets = [b for b, c in sorted(buckets.items()) if c >= threshold]

    # Merge consecutive spike windows into a single incident.
    incidents: list[dict] = []
    run: list[tuple[str, int]] = []
    prev: str | None = None
    for bucket in spike_buckets:
        if prev is not None and not _consecutive(prev, bucket):
            incidents.append(_spike_incident(run, baseline))
            run = []
        run.append((bucket, buckets[bucket]))
        prev = bucket
    if run:
        incidents.append(_spike_incident(run, baseline))
    return incidents


def _consecutive(a: str, b: str) -> bool:
    """True if bucket b is the minute right after bucket a (YYYY-MM-DDTHH:MM)."""
    try:
        ta = datetime.strptime(a, "%Y-%m-%dT%H:%M")
        tb = datetime.strptime(b, "%Y-%m-%dT%H:%M")
    except ValueError:
        return False
    return (tb - ta).total_seconds() == 60


def _spike_incident(run: list[tuple[str, int]], baseline: int) -> dict:
    total = sum(c for _, c in run)
    window = run[0][0] if len(run) == 1 else f"{run[0][0]}..{run[-1][0]}"
    return {
        "name": "Error-rate spike",
        "severity": "high",
        "occurrences": total,
        "affected": [f"log window {window}"],
        "recommended_action": (
            f"{total} error lines in the {window} window vs a "
            f"baseline of ~{baseline}/min. Correlate with deploys or "
            "traffic changes around that time; check the dominant "
            "error message in that window."
        ),
    }


# ------------------------------------------------------------- summaries


def heuristic_summary(incidents: list[dict]) -> str:
    lines = ["# sentinelops incident summary (heuristic mode)", ""]
    for inc in incidents:
        lines.append(f"## [{inc['severity'].upper()}] {inc['name']} x{inc['occurrences']}")
        lines.append(f"- Affected: {', '.join(inc['affected']) or 'n/a'}")
        lines.append(f"- Recommended action: {inc['recommended_action']}")
        lines.append("")
    return "\n".join(lines)


def ai_summary(incidents: list[dict]) -> str:
    try:
        import anthropic
    except ImportError:
        return "ERROR: 'anthropic' package not installed. Run: pip install -r requirements.txt"
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return "ERROR: ANTHROPIC_API_KEY is not set. Copy .env.example to .env and add your key."
    client = anthropic.Anthropic(api_key=api_key)
    prompt = (
        "You are an on-call SRE. Given these detected Kubernetes incidents "
        "(JSON), write a concise incident summary: likely blast radius, "
        "probable relationships between incidents, and a prioritized action "
        "plan with kubectl commands.\n\n" + json.dumps(incidents, indent=2)
    )
    message = client.messages.create(
        model="claude-sonnet-4-5-20250929",
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in message.content if getattr(b, "type", "") == "text")


# ---------------------------------------------------------------- slack


def slack_payload(incidents: list[dict], summary: str) -> dict:
    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f":rotating_light: *sentinelops detected {len(incidents)} incident(s)*"},
        }
    ]
    for inc in incidents:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*{inc['severity'].upper()}* - {inc['name']} x{inc['occurrences']}\n"
                    f"Affected: {', '.join(inc['affected']) or 'n/a'}",
                },
            }
        )
    blocks.append(
        {"type": "context", "elements": [{"type": "mrkdwn", "text": summary[:280]}]}
    )
    return {"text": f"sentinelops: {len(incidents)} incident(s) detected", "blocks": blocks}


def notify_slack(payload: dict, dry_run: bool) -> None:
    if dry_run:
        print("--- DRY RUN: payload that WOULD be sent to Slack ---")
        print(json.dumps(payload, indent=2))
        print("--- end of payload ---")
        print("Set SLACK_WEBHOOK_URL and pass --notify to send for real.")
        return
    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook:
        print("SLACK_WEBHOOK_URL is not set. Copy .env.example to .env.", file=sys.stderr)
        sys.exit(2)
    import requests

    resp = requests.post(webhook, json=payload, timeout=15)
    if resp.status_code != 200:
        print(f"Slack post failed: HTTP {resp.status_code} {resp.text[:200]}", file=sys.stderr)
        sys.exit(2)
    print("Incident summary posted to Slack.")


# ------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="K8s anomaly detector + Slack alerts.")
    parser.add_argument("command", choices=["detect"], help="Action to run.")
    parser.add_argument("events", nargs="?", help="Path to events JSONL file (omit with --live).")
    parser.add_argument("--live", action="store_true", help="Read live events via kubectl.")
    parser.add_argument("--logs", help="Container log file for error-rate spike detection.")
    parser.add_argument("--mode", choices=["heuristic", "ai"], default="heuristic")
    parser.add_argument("--notify", action="store_true", help="Actually post to Slack (default: dry run).")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if not args.live and not args.events:
        print("Provide an events file or use --live.", file=sys.stderr)
        return 1
    if args.events and not os.path.isfile(args.events):
        print(f"Events file not found: {args.events}", file=sys.stderr)
        return 1

    events = load_events(args.events, args.live)
    incidents = detect_event_anomalies(events)
    if args.logs:
        incidents += detect_log_spikes(args.logs)

    if not incidents:
        print("No anomalies detected.")
        return 0

    summary = ai_summary(incidents) if args.mode == "ai" else heuristic_summary(incidents)
    if args.json:
        print(json.dumps({"incidents": incidents, "summary": summary}, indent=2))
    else:
        print(summary)

    notify_slack(slack_payload(incidents, summary), dry_run=not args.notify)
    return 0


if __name__ == "__main__":
    sys.exit(main())
