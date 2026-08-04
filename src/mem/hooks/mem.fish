# mem shell hook for fish — install with: mem init fish | source
#
# How it works:
# - fish_postexec fires after each command with the full command line in $argv.
# - $status gives the exit code, $CMD_DURATION gives duration in milliseconds.
# - `& disown` backgrounds the capture so it never blocks the prompt.

function _mem_postexec --on-event fish_postexec
    set -l exit_code $status
    # $CMD_DURATION is set by fish automatically (milliseconds)
    command mem _capture "$argv" "$PWD" "$exit_code" "$CMD_DURATION" 2>/dev/null &
    disown 2>/dev/null
end
