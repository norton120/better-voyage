# 00 — Vision

**Status:** draft

## Problem

Sailors plan passages with a multitude of inputs: 

- marine forecasts for wind (speed + direction) and waves (swell, direction, & period), temperature, visibility, percipitation etc. 

- Tidal & current tables for water depth, direction, and current speed

- Marine charts 

- Experience and preferences of the skipper and the crew

- Attributes, strengths and weaknesses of the vessel

Tools like PredictWind combine some of this information in a very generic fashion, but fall short of what should be really possible today. 

- 

`better-voyage` focuses on more comprehensive, context-aware navigation assistance. Our software will go beyond raw numbers and intelligently answer this type of navigation request:

> **"I want to take this vessel from A to B sometime in the next N days. I am single handing and want to optimize for long tacks. When should I leave, what is the route, what does the passage look like, and what's my Plan B if it goes sideways?"**

## Who it's for

- Cruising sailors with a multi-day window and a rough destination.
- OpenCPN users who want a GPX route file they can drop into their plotter.
- Users who are often **offline** during passage and need their
  plan to survive without connectivity.

## Core user story

> As a skipper, I give the app a start point, an end point, and a time window. It returns a ranked list of candidate departure times, each with a GPX route, a scored simulation of the passage, a plain-English summary, and a set of named
> bailout points along the way.

## Success criteria (MVP)

1. User submits `{start, end, window}` and receives ≥3 ranked candidates.
2. Each candidate includes leg-by-leg forecast data and scored metrics.
3. Each candidate has at least one identified tap-out point for any leg > 4h.
4. Output is a valid GPX file that loads cleanly in OpenCPN.
5. All forecast and tide data used is cached locally; repeat queries work
   offline.
6. Basic GUI allows user to create routes, view routes, and select route to export to GPX.

## Inputs

- polars file

- boat data file: height, draft, beam

- crew headcount

- preferences: 
  
  - athletic vs relaxed voyage
  
  - sunlight sailing only, or night sails too
  
  - optimize for tack frequency, speed, overall distance, conditions
  
  - for stops:
    
    - include paid marinas

- Specific waypoints that must be included in the routes (multi-hop)

## Explicitly out of scope (for MVP)

- User accounts, multi-user state, cloud sync 
- Mobile app, push notifications (this will ONLY run locally and output GPX files)
- Interactive map/chart plotter functionality - this is a static GPX generator
- Commercial AIS/GRIB sources, paid forecast providers 

## Non-goals

- Becoming a PredictWind or Avalon Routing competitor.
- Optimizing for motoring or powerboat profiles (though not actively hostile).
- Functioning live underway - this is a planning tool, with routes intended to export to OpenCPN for use

## Reference

Avalon Offshore https://www.avalon-routing.com/wp-content/uploads/2026/01/Screenshot-2026-01-20-at-11.54.37-2-1024x715.jpg offers a product that has a lot of what we'd want to get out of our app: 

- the ability to predict the heel, speed, and sea state on a given tack of each leg of a generated route

- incorporates boat polars to make this accurate

- relies on openly available weather, tide and chart data 
