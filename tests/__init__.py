"""Test runners for the UDP/SRT throughput tester.

Each runner exposes a class with:
  - start(params, on_sample, on_done)  -> spawns subprocesses, streams samples
  - stop()                              -> cancels in-progress test

Samples are dicts with at least a 'ts' key plus mode-specific fields.
A final 'summary' dict is delivered via on_done.
"""
