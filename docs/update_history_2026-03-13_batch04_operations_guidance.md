# 2026-03-13 Batch 04 - Operations Guidance Document

## Why
- Strategy-specific refresh cadence was implemented, but the user also asked for clearer operating guidance.
- The repo needed a durable markdown reference describing which strategies fit stable real trading versus paper-first validation.

## What Changed
- Added an operations guidance markdown file:
  - `docs/strategy_operations_guidance_2026-03-13.md`
- Documented:
  - current real-trading defaults
  - strategy-specific refresh cadence
  - deployment suitability by strategy
  - current misconfiguration risks
  - recommended promotion order

## Notes
- This batch does not change live trading logic.
- It exists to make future configuration decisions easier to audit and, if needed, roll back conceptually.
