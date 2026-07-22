#!/bin/bash
set -e
cd /srv/private-ci/agent
exec /srv/private-ci/agent/venv/bin/python -m private_ci_agent.main
