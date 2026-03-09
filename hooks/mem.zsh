# mem shell hook — install with: eval "$(mem init zsh)"
#
# How it works:
# - _mem_preexec runs BEFORE each command, capturing the command text and start time
# - _mem_precmd runs AFTER each command, computing exit code and duration
# - mem _capture runs in the background (&!) so it NEVER blocks the prompt
#
# Why &! instead of &: In zsh, &! (or equivalently &|) disowns the process
# immediately, preventing "job completed" messages from appearing in the prompt.

_mem_preexec() {
  _mem_cmd="$1"
  _mem_start=$(date +%s%3N)
}

_mem_precmd() {
  local exit_code=$?
  if [[ -n "$_mem_cmd" ]]; then
    local end=$(date +%s%3N)
    local duration=$(( end - _mem_start ))
    mem _capture "$_mem_cmd" "$PWD" "$exit_code" "$duration" 2>/dev/null &!
    _mem_cmd=""
  fi
}

autoload -Uz add-zsh-hook
add-zsh-hook preexec _mem_preexec
add-zsh-hook precmd _mem_precmd
