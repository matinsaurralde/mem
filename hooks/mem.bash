# mem shell hook for bash — install with: eval "$(mem init bash)"
#
# How it works:
# - DEBUG trap fires before each simple command. We use _mem_capturing
#   as a guard so only the first simple command in a pipeline is recorded.
# - PROMPT_COMMAND runs after each command completes, capturing exit code
#   and duration, then calling mem _capture in the background.
# - `& disown` backgrounds the capture and suppresses job notifications.

_mem_cmd=""
_mem_start=0
_mem_capturing=""

_mem_debug_trap() {
  if [[ -z "$_mem_capturing" && -n "$BASH_COMMAND" ]]; then
    _mem_capturing=1
    _mem_cmd="$(HISTTIMEFORMAT= history 1 | sed 's/^[ ]*[0-9]*[ ]*//')"
    _mem_start=$SECONDS
  fi
}

_mem_prompt_cmd() {
  local exit_code=$?
  _mem_capturing=""
  if [[ -n "$_mem_cmd" ]]; then
    local duration=$(( (SECONDS - _mem_start) * 1000 ))
    mem _capture "$_mem_cmd" "$PWD" "$exit_code" "$duration" 2>/dev/null &
    disown 2>/dev/null
    _mem_cmd=""
  fi
}

trap '_mem_debug_trap' DEBUG
PROMPT_COMMAND="_mem_prompt_cmd${PROMPT_COMMAND:+;$PROMPT_COMMAND}"
