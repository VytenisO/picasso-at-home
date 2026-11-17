"""Web UI server for the pen plotter: HTTP API, job runner, event stream.

Layout (see docs/ui-design.md):
  config     paths and settings
  auth       password + session cookie
  events     sequence-numbered event log for the live stream
  hardware   read-only plotter check and the last-resort pen-up/motors-off
  runner     the one job subprocess at a time, its state machine and control commands
  plotjob    settings validation, plot_svg.py argv, preview/preflight, plan ids
  library    uploaded files (manifest) and per-file saved settings
  converters upload converters (registry)
  tools      test sheets, result recording and the pen test (registry)
  api        HTTP routes
"""
