#!/bin/bash

# run this command after git cloning to set up pre-commit hooks

echo $"#/bin/sh\nblack .\npython lint.py -p ../projectName/'> .git/hooks/pre-commit
chmod +x .git/hooks/pre-commit