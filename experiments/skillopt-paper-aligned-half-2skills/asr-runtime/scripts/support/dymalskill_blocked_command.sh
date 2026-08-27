#!/usr/bin/env bash
set -u

command_name="${0##*/}"

case "${command_name}" in
  python|python3|python3.12)
    if [[ "${1:-}" == "-m" && "${2:-}" =~ ^(pip|ensurepip)$ ]]; then
      printf 'blocked in isolated DyMalSkill trial: %s %s\n' "${command_name}" "$*" >&2
      exit 126
    fi
    exec /opt/python/bin/python "$@"
    ;;
esac

printf 'blocked in isolated DyMalSkill trial: %s\n' "${command_name}" >&2
exit 126
