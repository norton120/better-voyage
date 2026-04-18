# 09 — GPX output for OpenCPN

**Status:** draft

 This doc covers serializer conventions, namespace handling, and OpenCPN-specific niceties.

## Serializer

- `gpxpy` owns the base tree. Our Pydantic `Voyage` / `Route` /
  `Waypoint` models have `to_gpxpy()` / `from_gpxpy()` methods that
  round-trip losslessly.
- `<extensions>` are handled by a small wrapper that serializes our
  typed `Extensions` sub-model under the `bv:` namespace and preserves
  the `raw: dict[str, Any]` bag for unknown children (including
  foreign namespaces like `opencpn:`, `gpxx:`).

## Validation

- Output validates against the GPX 1.1 XSD
  (<http://www.topografix.com/GPX/1/1/gpx.xsd>).
- This is a test-suite check (doc 12), not a runtime check.

## Namespaces

```xml
<gpx version="1.1"
     creator="better-voyage/0.1"
     xmlns="http://www.topografix.com/GPX/1/1"
     xmlns:bv="https://better-voyage.app/gpx/1">
```

Foreign namespaces on inbound documents are preserved and re-emitted
unchanged. We never strip `opencpn:viz`, `gpxx:*`, etc.

## OpenCPN `<sym>` conventions

OpenCPN renders whichever built-in icon matches `<sym>` exactly;
unknown values fall back to a default dot. Our emitter uses this
vocabulary:

| Our usage        | `<sym>` value |
| ---------------- | ------------- |
| Anchorage POI    | `Anchor`      |
| Marina POI       | `Marina`      |
| Harbor of refuge | `Harbor`      |
| Hazard / shoal   | `Shoal`       |
| Inlet            | `Inlet`       |
| Generic waypoint | `Waypoint`    |

Inbound `<sym>` values we don't recognize pass through verbatim — we
never normalize them on write.

## Files emitted per voyage

- `voyage-{id}-candidate-{rank}.gpx` — a single candidate and its
  contingencies, for users who only want the winner.

## Deterministic element order

1. `<metadata>` (with `<bv:request>`, `<bv:coverage>` inside
   `<extensions>`).
2. `<wpt>` in lexicographic order by `name`.
3. `<rte>` by `bv:rank` ascending; contingency routes grouped after
   their parent candidate.

Stable order → file diffs are meaningful in tests.

## Delivery

- `GET /voyages/{id}/gpx` → master file.
- `GET /voyages/{id}/gpx?candidate={rank}` → single candidate.
- `Content-Type: application/gpx+xml`
- `Content-Disposition: attachment; filename="voyage-{id}.gpx"`

## Round-trip contract

If a user `POST`s a previously-emitted GPX back to the planner
(optionally with edits), the parser reads every standard field and
every `bv:*` extension untouched. No field introduced in a previous
version is silently dropped.

## Notes

- we emit `<bounds>` in `<metadata>`
-  we  do not round `<ele>` / `<time>` precision to save bytes
