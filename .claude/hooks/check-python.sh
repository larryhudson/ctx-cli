#!/bin/bash
# Post-edit hook to run ruff and ty checks on modified Python files

# Read the hook input from stdin
INPUT=$(cat)

# Extract the modified file path using jq
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')

# Only run checks on Python files
if [[ ! "$FILE_PATH" =~ \.py$ ]]; then
  exit 0
fi

# Check if file exists (might have been deleted)
if [[ ! -f "$FILE_PATH" ]]; then
  exit 0
fi

# Auto-format (safe - only style changes, never removes code)
# Claude Code automatically notifies about file changes
uv run ruff format "$FILE_PATH" >/dev/null 2>&1

HAS_ERRORS=0
OUTPUT=""

# Run ruff lint check (report only, no --fix to preserve unused imports mid-edit)
LINT_OUTPUT=$(uv run ruff check "$FILE_PATH" 2>&1)
if [[ $? -ne 0 ]]; then
  HAS_ERRORS=1
  OUTPUT+="Lint issues:\n$LINT_OUTPUT\n\n"
fi

# Run ty type check
TYPE_OUTPUT=$(uv run ty check "$FILE_PATH" 2>&1)
if [[ $? -ne 0 ]]; then
  HAS_ERRORS=1
  OUTPUT+="Type errors:\n$TYPE_OUTPUT\n\n"
fi

# If there were errors, output to stderr and exit with code 2
if [[ $HAS_ERRORS -eq 1 ]]; then
  echo -e "--- Errors in $FILE_PATH ---\n$OUTPUT" >&2
  exit 2
fi

exit 0
