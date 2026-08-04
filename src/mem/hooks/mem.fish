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

# --- Ctrl+R ------------------------------------------------------------------
#
# The finder prints the chosen command to stdout and draws its interface on
# /dev/tty, so the substitution below captures the choice and nothing else.
# The command is placed on the command line, never executed: you get to read
# it, edit it, and decide. A history search that runs things behind your back
# is how people delete the wrong branch.
#
# Set MEM_NO_KEYBINDING=1 before loading this hook to keep fish's own Ctrl+R.

function _mem_search_widget
    set -l selected (command mem tui -- (commandline) </dev/tty)
    if test $status -eq 0 -a -n "$selected"
        commandline -r -- "$selected"
        commandline -f end-of-line
    end
    commandline -f repaint
end

if not set -q MEM_NO_KEYBINDING
    bind \cr _mem_search_widget
    # fish keeps a separate binding table for vi mode; without this, Ctrl+R
    # silently keeps the built-in search for anyone using it.
    bind -M insert \cr _mem_search_widget 2>/dev/null
end
