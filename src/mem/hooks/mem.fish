# mem shell hook for fish — install with: mem init fish | source
#
# How it works:
# - fish_postexec fires after each command with the full command line in $argv.
# - $status gives the exit code, $CMD_DURATION gives duration in milliseconds.
# - `& disown` backgrounds the capture so it never blocks the prompt.
#
# fish is the shell that already got the hard parts right: $argv is the whole
# command line (no pipeline truncation) and $CMD_DURATION is already in
# milliseconds (no integer-seconds clock). The zsh and bash hooks were brought
# up to this behaviour, not the other way around.

function _mem_postexec --on-event fish_postexec
    # Must be the very first statement: anything before it clobbers $status.
    set -l exit_code $status
    set -l cmd "$argv"

    # A leading space is the universal "do not record this" gesture. fish keeps
    # such lines out of its own history; mem honours the same gesture, because
    # its store is searchable and fed to an AI layer and is therefore strictly
    # more sensitive than a history file.
    if string match -q -- ' *' $cmd; or string match -q -- \t'*' $cmd
        return
    end

    # $CMD_DURATION is set by fish automatically (milliseconds), but it is unset
    # before the first command of a session.
    set -l duration $CMD_DURATION
    if test -z "$duration"
        set duration 0
    end

    command mem _capture "$cmd" "$PWD" "$exit_code" "$duration" 2>/dev/null &
    disown 2>/dev/null
end
