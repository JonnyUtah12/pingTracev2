# Changelog

All notable changes to pingTracev2 are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions are tagged `vX.Y.Z` and the portable Windows zip is built by the
`windows-portable` workflow on each tag.

## [Unreleased]

### Added
- Per-hop time series (GAP-1 slice 1): every stored trace run now records one
  `hop_samples` row per hop (responder address, mean answered-probe RTT or
  NULL when the hop was silent, probe/answered counts). Schema v5; existing
  databases upgrade in place.
- Read-only API `GET /api/targets/{id}/hop-history?range=&bucket=`: per-hop
  bucketed latency/loss series with the same null-vs-latency semantics as
  `/api/history` (timeouts are never 0 ms; empty buckets are gaps), plus the
  responder addresses seen at each hop index in the window so the dashboard
  can label rows and flag route changes.
- `hop_samples` rows are pruned by the same retention floor as rollups and
  removed with their target; the stats block reports the row count.

### Changed
- Migration tests assert `database.SCHEMA_VERSION` instead of a hardcoded
  version string, so future schema bumps don't break older migration tests.
