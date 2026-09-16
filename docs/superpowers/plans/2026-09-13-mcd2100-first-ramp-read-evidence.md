# First user-operated ramp-table read: evidence and bounded correction

User supplied App log and two cached-table screenshots from 2026-09-13 02:18:00 -04:00. Agent did not operate the App or instrument.

## Observed results

- Manual telemetry log: completed in 8.043 s at 02:17:54.
- Automatic ramp report: completed at 02:18:00.793909 -04:00; elapsed 6.744 s.
- Current reported count: 5; Default reported count: 5; channel: 0.

| Table | Requested index | Raw range | Raw rate | Error |
| --- | ---: | ---: | ---: | --- |
| Current | 0 | 40 | 0.0344 | none |
| Current | 1 | 44.0116 | 0.0172 | none |
| Current | 2 | 45 | 0.0172 | none |
| Current | 3 | 46 | 0.0086 | none |
| Current | 4 | unavailable | unavailable | VALUEOUTOFRANGETOOHIGH, 11 |
| Default | 0 | 40 | 0.0344 | none |
| Default | 1 | unavailable | unavailable | HARDWARENOTAVAILABLE, 9 |
| Default | 2 | unavailable | unavailable | HARDWARENOTAVAILABLE, 9 |
| Default | 3 | unavailable | unavailable | VALUEOUTOFRANGE, 10 |
| Default | 4 | unavailable | unavailable | VALUEOUTOFRANGE, 10 |

Five failed getter items appear twice each in the summary. This does not show ten separate failing SDK calls or an automatic retry. After-read telemetry recovery and UI responsiveness are not established by these two screenshots and three log lines.

## Confirmed App defect and executable repair

Adapter `read_ramp_tables.read_table` preserves each row error both in `row.error` and in `table.errors`. Panel `_poll_ramp_tables` concatenates both lists without deduplication. Preserve the raw report contract; repair only summary aggregation.

Luna: collect unique complete messages within each table in original order, combining table-level and row-only errors. Reset the seen set for each table so separate Current and Default failures are not collapsed. Use this list consistently in log, latest-error metadata and count. Do not alter getter calls, row records, counts or SDK argument order.

Acceptance: the user-shaped report yields five failures with each full message once; table-level-only and row-only errors remain; equal messages in different tables remain distinct table failures; raw reports and getter call counts unchanged. Run affected panel/ramp tests, then Astra inspects only this correction and its regression evidence.

Correction complete: Luna implemented panel-only ordered per-table summary deduplication, preserving adapter structures and calls. Luna ran 13 ramp-focused and 59 panel tests; root Astra inspected the aggregation path and independently ran both added error-summary regressions, 2/2 passing. No getter/order/index/unit change or real-device operation occurred. Existing user-running App was not restarted by agents.

## Subsequent user report

At 02:19:53 the user reported Prepare rejected with `attoDRY2100 owner work is still draining`. This message is emitted by panel `prepare_magnet()` for any controller pending request before `_launch_worker` disables polling. It does not distinguish a normal ongoing display read from an abnormally undrained request, and alone does not prove the earlier ramp report is still pending. A separate real-controller/gated-fake diagnosis is underway; no Stop, disconnection or real-device action is used to investigate.

## Documentation evidence versus unresolved behavior

- System specification `01_220507_System-Spec-Sheet.pdf`, PDF p5: APS100. PDF p8: 2044.9 G/A; 0.0344 A/s over 0–40 A and 0.0172 A/s over 40–44.0116 A. The first two Current values match numerically. Do not treat the extra raw limits 45/46 as approval to exceed the magnet's specified operating range.
- `04_APS100 magnet power supply manual v1d1.pdf`, PDF p16: up to five normal current ranges plus a fast rate; Rates menu uses A/s. PDF p51: native APS `RANGE?` upper boundaries in amps. PDF p52: native APS `RATE?` uses A/s and range selectors 0–4 for normal ranges, 5 for Fast. This is the native APS interface, not proof that the attoDRY SDK index maps identically.
- Local vendor `CRYO2100/magnet.py`: `getRampRate(channel,index)` transmits `[channel,index]`; `getDefaultRampRate(index,channel)` transmits `[index,channel]`. The App follows those SDK declarations. The SDK wrappers do not explain the count/getter mismatch or firmware implementation.

Current count=5 with index 4 rejected is an observed API discrepancy, not evidence that an index-base change or a fabricated fifth boundary is correct. Default index-dependent hardware/range errors make SDK/firmware argument compatibility a hypothesis worth checking, but do not prove reversal is required or hardware is physically faulty. No parameter-order fallback, index probing, writes or inferred unit labels are introduced.

Needed evidence before compatibility changes: device firmware/API version and its matching official API signature/schema; manufacturer clarification of Default argument order and count versus accessible entries; any user-provided subsequent telemetry logs. Existing cached values can be inspected without another read. No further instrument test is executed by agents.
