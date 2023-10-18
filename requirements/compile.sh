#!/bin/bash

# See CONTRIBUTING.md for description

pip-compile --no-header -o requirements.txt ./../pyproject.toml
pip-compile --no-header --extra dev -o dev_requirements.txt ./../pyproject.toml
pip-compile --no-header --extra test -o test_requirements.txt ./../pyproject.toml
pip-compile --no-header --extra dev --extra test -o all_requirements.txt ./../pyproject.toml
