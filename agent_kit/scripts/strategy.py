"""Hosted trading strategies: info, draft, validate, preview, start, status and stop.

The commands live in workflow.py, so `python -m agent_kit.scripts.workflow` keeps
working as an alias of `python -m agent_kit.scripts.strategy`.
"""

from agent_kit.scripts.workflow import main

if __name__ == "__main__":
    main()
