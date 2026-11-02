# Time Desk eval report

| Case | tool_trajectory_avg_score | response_match_score |
|---|---|---|
| tokyo_to_kolkata | 1.00 | 1.00 |
| singapore_to_kathmandu | 1.00 | 1.00 |
| dubai_to_karachi | 1.00 | 1.00 |
| off_topic_no_tools | 1.00 | 0.92 |
| missing_zone_asks | 1.00 | 0.43 |

| Metric | Mean | Threshold | Result |
|---|---|---|---|
| tool_trajectory_avg_score | 1.00 | 1.0 | pass |
| response_match_score | 0.87 | 0.6 | pass |

Meter and checks printed to stderr by the same run:

```
— 9.5s · 8 model calls · 3/30 tool calls {'convert_time': 3} · 10,636 in / 205 out tokens
✓ tool_trajectory_avg_score = 1.00 (threshold 1.0)
✓ response_match_score = 0.87 (threshold 0.6)
```
