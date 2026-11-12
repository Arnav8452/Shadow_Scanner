"""Cockpit: live engine state pushed to a local browser page (no files, no refresh).

Processes (docs/RUNBOOK.md "Cockpit"):
- P1 recorder (``tasks.py record``): unchanged, the system of record.
- P2 engine (``tasks.py shadow-live``): order books, scanner, paper tracks A/B/C, 30-level
  recording. Publishes snapshots and takes paper orders over an authenticated local socket.
- P3 gateway (``tasks.py cockpit``): the web page and WebSocket, research views, replays.
- P4 replay (``tasks.py replay``): the same engine on recorded data, own state and journals.
"""
