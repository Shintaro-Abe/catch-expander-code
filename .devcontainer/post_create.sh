#!/bin/bash
npm install -g @anthropic-ai/claude-code

# uv (Python package manager) - required for deploy-on-aws plugin MCP servers
curl -LsSf https://astral.sh/uv/install.sh | sh
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.profile