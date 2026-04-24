# better-voyage — Plan

This directory captures the design of `better-voyage` before it is built.
Each document is small, focused, and intended to be edited as decisions
change. If you are here to implement a feature, start at `00-vision.md` and
work outward.

## Index

| #   | Doc                                                 | Purpose                                               |
| --- | --------------------------------------------------- | ----------------------------------------------------- |
| 00  | [vision.md](./00-vision.md)                         | Problem statement, user stories, success criteria     |
| 01  | [domain-model.md](./01-domain-model.md)             | Core entities and relationships                       |
| 02  | [architecture.md](./02-architecture.md)             | Layers, packages, dependencies, async model           |
| 03  | [data-sources.md](./03-data-sources.md)             | Weather & tide APIs + charts (ENC, OSM, GEBCO) + POIs |
| 04  | [routing.md](./04-routing.md)                       | Leg construction, waypoints, POI snapping             |
| 05  | [scoring.md](./05-scoring.md)                       | Leg-level and passage-level scoring model             |
| 06  | [contingencies.md](./06-contingencies.md)           | Plan Bs, tap-outs, escape-hatch routes                |
| 07  | [windows-simulation.md](./07-windows-simulation.md) | Enumerating and simulating departure windows          |
| 08  | [nl-summary.md](./08-nl-summary.md)                 | Natural-language pros/cons per candidate              |
| 09  | [gpx-output.md](./09-gpx-output.md)                 | GPX emission for OpenCPN                              |
| 10  | [api.md](./10-api.md)                               | REST surface, request/response shapes                 |
| 11  | [storage-caching.md](./11-storage-caching.md)       | SQLite schema, cache policy, offline behavior         |
| 12  | [testing.md](./12-testing.md)                       | Unit, integration, replay-based testing               |
| 13  | [roadmap.md](./13-roadmap.md)                       | Milestones, MVP cut, stretch ideas                    |
| 14  | [observability.md](./14-observability.md)           | Logs, traces, metrics, plan audit trail               |
| 15  | [jobs-async.md](./15-jobs-async.md)                 | Async job model: states, progress, lifecycle          |
| 17  | [isochrone-overhaul.md](./17-isochrone-overhaul.md) | Diagnosis + plan to replace sector-prune with Normalize/Merge |

## Conventions

- **Status**: every doc starts with a `Status:` line (`draft`, `stable`, `revisiting`).
- **Cross-refs**: link to other docs rather than duplicating content.
- **Units**: SI unless otherwise stated. Speeds in knots, distances in nautical
  miles (nm), times in UTC internally, presented in the user's local zone.
