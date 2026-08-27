#!/usr/bin/env bash
set -Eeuo pipefail

readonly SCRIPT_PATH="$(readlink -f -- "${BASH_SOURCE[0]}")"
readonly SCRIPT_DIR="$(dirname -- "${SCRIPT_PATH}")"
readonly SUPPORT_DIR="${SCRIPT_DIR}/support"
readonly EXPERIMENT_DIR="$(dirname -- "${SCRIPT_DIR}")"
readonly V3_PIPELINE_HOST="${EXPERIMENT_DIR}/stage7_v3_pipeline.py"
readonly V3_BEHAVIOR_VERIFIER_HOST="${EXPERIMENT_DIR}/stage7_v3_behavior_verifier.py"
readonly V3_VERIFIER_BUNDLE_HOST="${EXPERIMENT_DIR}/vendor/skillsandbox_verifier_12"

die() {
  printf 'run_isolated_dymalskill_trial: %s\n' "$*" >&2
  exit 2
}

runner_cleanup() {
  local pid="${DYMAL_BRIDGE_PID:-}"
  if [[ -n "${pid}" ]]; then
    kill -TERM "${pid}" 2>/dev/null || true
    wait "${pid}" 2>/dev/null || true
  fi
}

run_probe() {
  /opt/python/bin/python - <<'PY'
import json
import os
import pty
import socket
import subprocess
from pathlib import Path


def write_must_fail(path: Path) -> bool:
    try:
        path.write_text("isolation violation\n", encoding="utf-8")
    except OSError:
        return True
    return False


def command_is_blocked(argv: list[str]) -> bool:
    result = subprocess.run(argv, check=False, capture_output=True, text=True, timeout=10)
    return result.returncode == 126 and "blocked in isolated DyMalSkill trial" in result.stderr


def command_cannot_execute(argv: list[str]) -> bool:
    try:
        result = subprocess.run(argv, check=False, capture_output=True, text=True, timeout=10)
    except (FileNotFoundError, PermissionError):
        return True
    return result.returncode != 0


def command_succeeds(argv: list[str]) -> bool:
    result = subprocess.run(argv, check=False, capture_output=True, text=True, timeout=10)
    return result.returncode == 0


stage = Path("/output/staging")
write_probe = stage / "_isolation_probe_write.txt"
write_probe.write_text("staging-write-ok\n", encoding="utf-8")

cap_eff = None
no_new_privs = None
for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
    if line.startswith("CapEff:"):
        cap_eff = int(line.split()[1], 16)
    elif line.startswith("NoNewPrivs:"):
        no_new_privs = int(line.split()[1])

external = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
external.settimeout(1.0)
try:
    external_network_blocked = external.connect_ex(("1.1.1.1", 53)) != 0
finally:
    external.close()

interfaces = []
for line in Path("/proc/net/dev").read_text(encoding="utf-8").splitlines()[2:]:
    interfaces.append(line.split(":", 1)[0].strip())

private_pty_available = False
pty_master = None
pty_slave = None
try:
    pty_master, pty_slave = pty.openpty()
    private_pty_available = os.ttyname(pty_slave).startswith("/dev/pts/")
except OSError:
    pass
finally:
    for descriptor in (pty_master, pty_slave):
        if descriptor is not None:
            os.close(descriptor)

try:
    import openhands.sdk  # noqa: F401
except Exception as exc:
    openhands_import_error = f"{type(exc).__name__}: {exc}"
else:
    openhands_import_error = None

checks = {
    "capabilities_dropped": cap_eff == 0,
    "direct_git_blocked": command_cannot_execute(["/usr/bin/git", "--version"]),
    "direct_python_m_pip_blocked": command_cannot_execute(
        ["/opt/python/bin/python", "-m", "pip", "--version"]
    ),
    "dataset_read_only": write_must_fail(Path("/input/dataset/_isolation_write_test")),
    "environment_read_only": write_must_fail(Path("/opt/python/_isolation_write_test")),
    "evaluator_read_only": write_must_fail(Path("/opt/skillsandbox/_isolation_write_test")),
    "v3_pipeline_adapter_read_only": write_must_fail(
        Path("/opt/evaluator/stage7_v3_pipeline.py")
    ),
    "behavior_verifier_adapter_read_only": write_must_fail(
        Path("/opt/evaluator/stage7_v3_behavior_verifier.py")
    ),
    "behavior_verifier_bundle_read_only": write_must_fail(
        Path("/opt/verifier_12/_isolation_write_test")
    ),
    "external_network_blocked": external_network_blocked,
    "git_blocked": command_is_blocked(["git", "--version"]),
    "gpu_devices_absent": not any(Path("/dev").glob("nvidia*")),
    "host_home_hidden": not Path("/home/tc442").exists(),
    "host_work_hidden": not Path("/work/tc442").exists(),
    "network_loopback_only": interfaces == ["lo"],
    "nvidia_smi_blocked": command_is_blocked(["nvidia-smi"]),
    "no_new_privileges": no_new_privs == 1,
    "openhands_import": openhands_import_error is None,
    "pip_blocked": command_is_blocked(["pip", "--version"]),
    "python_execution_available": command_succeeds(
        ["python3", "-c", "from pathlib import Path; assert Path('/input/dataset').is_dir()"]
    ),
    "python_m_pip_blocked": command_is_blocked(["python3", "-m", "pip", "--version"]),
    "private_pty_available": private_pty_available,
    "staging_write": write_probe.read_text(encoding="utf-8") == "staging-write-ok\n",
}
payload = {
    "schema_version": 1,
    "passed": all(checks.values()),
    "checks": checks,
    "interfaces": interfaces,
    "openhands_import_error": openhands_import_error,
}
output = stage / "isolation_probe.json"
output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(json.dumps(payload, sort_keys=True))
raise SystemExit(0 if payload["passed"] else 1)
PY
}

run_inside_runner() {
  [[ "${DYMAL_INTERNAL_MARKER:-}" == "runner-v1" ]] || die "invalid internal runner invocation"
  [[ -S /bridge/model.sock ]] || die "model socket is not mounted"
  [[ -d /output/staging/output ]] || mkdir -p -- /output/staging/output

  /opt/python/bin/python /opt/evaluator/model_socket_bridge.py \
    --log-level INFO listen-tcp \
    --listen-host 127.0.0.1 \
    --listen-port "${DYMAL_SANDBOX_PORT}" \
    --connect-unix /bridge/model.sock \
    > /output/staging/model_bridge.log 2>&1 &
  DYMAL_BRIDGE_PID=$!
  export DYMAL_BRIDGE_PID
  trap runner_cleanup EXIT INT TERM

  local ready=0
  for _ in $(seq 1 100); do
    if /opt/python/bin/python - "${DYMAL_SANDBOX_PORT}" <<'PY' 2>/dev/null
import socket
import sys

port = int(sys.argv[1])
with socket.create_connection(("127.0.0.1", port), timeout=1.0) as connection:
    connection.sendall(
        b"GET /health HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
    )
    response = connection.recv(4096)
if b" 200 " not in response.split(b"\r\n", 1)[0]:
    raise SystemExit(1)
PY
    then
      ready=1
      break
    fi
    if ! kill -0 "${DYMAL_BRIDGE_PID}" 2>/dev/null; then
      printf 'namespace model bridge exited during readiness check\n' >&2
      return 1
    fi
    sleep 0.1
  done
  (( ready == 1 )) || { printf 'model-only bridge health check failed\n' >&2; return 1; }

  case "${DYMAL_EXECUTION_MODE}" in
    probe)
      run_probe
      ;;
    test-command)
      /bin/bash -Eeuo pipefail -c "${DYMAL_TEST_COMMAND}"
      ;;
    d3-probe)
      /opt/python/bin/python - <<'PY'
import shutil
import sys
from pathlib import Path

sys.path.insert(0, "/opt/evaluator")
from stage7_v3_pipeline import enforce_d3

source = Path("/input/dataset") / Path(__import__("os").environ["DYMAL_ATTACK"]) / Path(
    __import__("os").environ["DYMAL_SKILL_ID"]
)
target = (
    Path("/output/staging/output")
    / __import__("os").environ["DYMAL_ATTACK"]
    / __import__("os").environ["DYMAL_SKILL_ID"]
    / "workspace/.agents/skills"
    / __import__("os").environ["DYMAL_SKILL_ID"]
)
target.parent.mkdir(parents=True, exist_ok=True)
shutil.copytree(source, target)
enforce_d3(target)
PY
      ;;
    pipeline)
      timeout --signal=TERM --kill-after=30s \
        "$((DYMAL_TRIAL_TIMEOUT_SECONDS + 180))" \
        /opt/python/bin/python /opt/evaluator/stage7_v3_pipeline.py \
          --skills-dir /input/dataset \
          --attack "${DYMAL_ATTACK}" \
          --prompts-manifest /input/task_prompts.json \
          --skill-id "${DYMAL_SKILL_ID}" \
          --model "${DYMAL_MODEL}" \
          --base-url "http://127.0.0.1:${DYMAL_SANDBOX_PORT}/v1" \
          --force-activate-skills \
          --defense "${DYMAL_DEFENSE}" \
          --run-root /output/staging/output \
          --trial-timeout-seconds "${DYMAL_TRIAL_TIMEOUT_SECONDS}" \
          --attempt "${DYMAL_ATTEMPT}" \
          --no-copy-back
      ;;
    *)
      die "invalid execution mode: ${DYMAL_EXECUTION_MODE}"
      ;;
  esac
}

if [[ "${1:-}" == "--_runner" ]]; then
  shift
  (( $# == 0 )) || die "unexpected runner arguments"
  run_inside_runner
  exit $?
fi

bind_read_only() {
  local source="$1" target="$2" exec_flag="${3:-noexec}"
  if [[ -d "${source}" ]]; then
    mkdir -p -- "${target}"
  else
    mkdir -p -- "$(dirname -- "${target}")"
    touch -- "${target}"
  fi
  mount --bind -- "${source}" "${target}"
  if [[ "${exec_flag}" == "exec" ]]; then
    mount -o remount,bind,ro,nosuid,nodev -- "${target}"
  else
    mount -o remount,bind,ro,nosuid,nodev,noexec -- "${target}"
  fi
}

mask_executable() {
  local target="$1"
  [[ -e "${target}" ]] || return 0
  mount --bind /dev/null "${target}"
  mount -o remount,bind,ro,nosuid,nodev,noexec -- "${target}"
}

mask_python_module() {
  local target="$1" empty_root="$2"
  [[ -d "${target}" ]] || return 0
  mkdir -p -- "${empty_root}"
  mount --bind -- "${empty_root}" "${target}"
  mount -o remount,bind,ro,nosuid,nodev,noexec -- "${target}"
}

assert_mount_option() {
  local target="$1" expected="$2" options
  options="$(findmnt -n -o OPTIONS --target "${target}")"
  [[ ",${options}," == *",${expected},"* ]] \
    || die "mount ${target} lacks required option ${expected}: ${options}"
}

validate_d3_mount_request() {
  local request_path="$1" nonce="$2" expected_relative="$3"
  [[ -f "${request_path}" && ! -L "${request_path}" ]] || return 1
  /usr/bin/python3 - "${request_path}" "${nonce}" "${expected_relative}" <<'PY'
import json
import sys

path, nonce, expected_relative = sys.argv[1:]
with open(path, encoding="utf-8") as stream:
    value = json.load(stream)
expected = {
    "schema_version": 1,
    "nonce": nonce,
    "relative_path": expected_relative,
}
if not isinstance(value, dict) or any(value.get(key) != item for key, item in expected.items()):
    raise SystemExit(1)
pid = value.get("pid")
if type(pid) is not int or pid <= 0:
    raise SystemExit(1)
PY
}

write_d3_mount_ack() {
  local ack_path="$1" nonce="$2" expected_relative="$3" mount_options="$4"
  /usr/bin/python3 - "${ack_path}" "${nonce}" "${expected_relative}" "${mount_options}" <<'PY'
import json
import os
import sys

path, nonce, expected_relative, mount_options = sys.argv[1:]
temporary = f"{path}.{os.getpid()}.tmp"
value = {
    "schema_version": 1,
    "nonce": nonce,
    "relative_path": expected_relative,
    "status": "mounted_read_only",
    "mount_options": mount_options.split(","),
}
with open(temporary, "x", encoding="utf-8") as stream:
    json.dump(value, stream, indent=2, sort_keys=True)
    stream.write("\n")
    stream.flush()
    os.fsync(stream.fileno())
os.replace(temporary, path)
PY
}

run_inside_namespace() {
  [[ "${DYMAL_INTERNAL_MARKER:-}" == "namespace-v1" ]] || die "invalid namespace invocation"
  [[ "$(id -u)" == "0" ]] || die "user namespace did not map caller to root"

  mount --make-rprivate /
  mount -t tmpfs -o mode=0755,nosuid,nodev tmpfs "${DYMAL_MOUNT_ROOT}"

  mkdir -p -- \
    "${DYMAL_MOUNT_ROOT}/bridge" \
    "${DYMAL_MOUNT_ROOT}/dev/shm" \
    "${DYMAL_MOUNT_ROOT}/dev/pts" \
    "${DYMAL_MOUNT_ROOT}/etc" \
    "${DYMAL_MOUNT_ROOT}/home" \
    "${DYMAL_MOUNT_ROOT}/input" \
    "${DYMAL_MOUNT_ROOT}/opt/evaluator" \
    "${DYMAL_MOUNT_ROOT}/opt/shims" \
    "${DYMAL_MOUNT_ROOT}/output/staging" \
    "${DYMAL_MOUNT_ROOT}/proc" \
    "${DYMAL_MOUNT_ROOT}/run" \
    "${DYMAL_MOUNT_ROOT}/tmp" \
    "${DYMAL_MOUNT_ROOT}/usr" \
    "${DYMAL_MOUNT_ROOT}/var"

  ln -s usr/bin "${DYMAL_MOUNT_ROOT}/bin"
  ln -s usr/sbin "${DYMAL_MOUNT_ROOT}/sbin"
  ln -s usr/lib "${DYMAL_MOUNT_ROOT}/lib"
  ln -s usr/lib64 "${DYMAL_MOUNT_ROOT}/lib64"
  ln -s /tmp "${DYMAL_MOUNT_ROOT}/var/tmp"

  bind_read_only /usr "${DYMAL_MOUNT_ROOT}/usr" exec
  bind_read_only /etc "${DYMAL_MOUNT_ROOT}/etc"
  bind_read_only "${DYMAL_PYTHON_ENV}" "${DYMAL_MOUNT_ROOT}/opt/python" exec
  bind_read_only "${DYMAL_SKILLSANDBOX_ROOT}" "${DYMAL_MOUNT_ROOT}/opt/skillsandbox"
  bind_read_only "${DYMAL_DATASET_ROOT}" "${DYMAL_MOUNT_ROOT}/input/dataset"
  bind_read_only "${DYMAL_PROMPT_MANIFEST}" "${DYMAL_MOUNT_ROOT}/input/task_prompts.json"
  bind_read_only "${DYMAL_SCRIPT_HOST}" "${DYMAL_MOUNT_ROOT}/opt/evaluator/run_isolated_dymalskill_trial.sh" exec
  bind_read_only "${DYMAL_BRIDGE_SCRIPT}" "${DYMAL_MOUNT_ROOT}/opt/evaluator/model_socket_bridge.py"
  bind_read_only "${DYMAL_BLOCKED_SCRIPT}" "${DYMAL_MOUNT_ROOT}/opt/evaluator/dymalskill_blocked_command.sh" exec
  bind_read_only "${DYMAL_V3_PIPELINE_HOST}" "${DYMAL_MOUNT_ROOT}/opt/evaluator/stage7_v3_pipeline.py"
  bind_read_only "${DYMAL_V3_BEHAVIOR_VERIFIER_HOST}" "${DYMAL_MOUNT_ROOT}/opt/evaluator/stage7_v3_behavior_verifier.py"
  bind_read_only "${DYMAL_V3_VERIFIER_BUNDLE_HOST}" "${DYMAL_MOUNT_ROOT}/opt/verifier_12"
  bind_read_only "${DYMAL_HOST_UNIX_SOCKET}" "${DYMAL_MOUNT_ROOT}/bridge/model.sock"

  local blocked_path
  for blocked_path in \
    "${DYMAL_MOUNT_ROOT}"/usr/bin/{apt,apt-cache,apt-get,brew,conda,curl,doas,dpkg,git,mamba,micromamba,npm,npx,nvidia-smi,pip,pip3,pip3.10,pkexec,pnpm,rsync,scp,sftp,snap,ssh,su,sudo,uv,uvx,wget,yarn} \
    "${DYMAL_MOUNT_ROOT}"/usr/local/bin/{brew,conda,curl,git,mamba,micromamba,nvidia-smi,pip,pip3,uv,uvx,wget} \
    "${DYMAL_MOUNT_ROOT}"/opt/python/bin/{conda,curl,git,mamba,micromamba,nvidia-smi,pip,pip3,pip3.12,uv,uvx,wget}; do
    mask_executable "${blocked_path}"
  done

  local module_path module_index=0
  shopt -s nullglob
  for module_path in \
    "${DYMAL_MOUNT_ROOT}"/opt/python/lib/python*/ensurepip \
    "${DYMAL_MOUNT_ROOT}"/opt/python/lib/python*/site-packages/pip \
    "${DYMAL_MOUNT_ROOT}"/usr/lib/python*/ensurepip \
    "${DYMAL_MOUNT_ROOT}"/usr/lib/python*/dist-packages/pip; do
    module_index=$((module_index + 1))
    mask_python_module \
      "${module_path}" \
      "${DYMAL_MOUNT_ROOT}/opt/blocked-modules/module-${module_index}"
  done
  shopt -u nullglob

  mount --bind -- "${DYMAL_STAGING_DIR}" "${DYMAL_MOUNT_ROOT}/output/staging"
  mount -o remount,bind,rw,nosuid,nodev -- "${DYMAL_MOUNT_ROOT}/output/staging"

  local command_name
  for command_name in \
    apt apt-cache apt-get brew conda curl doas dpkg git mamba micromamba \
    npm npx nvidia-smi pip pip3 pip3.12 pkexec pnpm python python3 python3.12 \
    rsync scp sftp snap ssh su sudo uv uvx wget yarn; do
    ln -s ../evaluator/dymalskill_blocked_command.sh \
      "${DYMAL_MOUNT_ROOT}/opt/shims/${command_name}"
  done

  mount -t proc -o nosuid,nodev,noexec proc "${DYMAL_MOUNT_ROOT}/proc"
  mount -t tmpfs -o mode=0755,nosuid,nodev,noexec tmpfs "${DYMAL_MOUNT_ROOT}/home"
  mkdir -p -- "${DYMAL_MOUNT_ROOT}/home/sandbox"
  chmod 0700 "${DYMAL_MOUNT_ROOT}/home/sandbox"
  mount -t tmpfs -o mode=1777,nosuid,nodev,noexec tmpfs "${DYMAL_MOUNT_ROOT}/tmp"
  mount -t tmpfs -o mode=0755,nosuid,nodev,noexec tmpfs "${DYMAL_MOUNT_ROOT}/run"
  mount -t tmpfs -o mode=0755,nosuid,nodev,noexec tmpfs "${DYMAL_MOUNT_ROOT}/dev"
  mkdir -p -- "${DYMAL_MOUNT_ROOT}/dev/shm" "${DYMAL_MOUNT_ROOT}/dev/pts"
  local device
  for device in full null random tty urandom zero; do
    touch -- "${DYMAL_MOUNT_ROOT}/dev/${device}"
    mount --bind -- "/dev/${device}" "${DYMAL_MOUNT_ROOT}/dev/${device}"
  done
  mount -t tmpfs -o mode=1777,nosuid,nodev,noexec tmpfs "${DYMAL_MOUNT_ROOT}/dev/shm"
  mount -t devpts -o newinstance,ptmxmode=0666,mode=0620,nosuid,noexec \
    devpts "${DYMAL_MOUNT_ROOT}/dev/pts"
  ln -s /proc/self/fd "${DYMAL_MOUNT_ROOT}/dev/fd"
  ln -s pts/ptmx "${DYMAL_MOUNT_ROOT}/dev/ptmx"
  ln -s /proc/self/fd/0 "${DYMAL_MOUNT_ROOT}/dev/stdin"
  ln -s /proc/self/fd/1 "${DYMAL_MOUNT_ROOT}/dev/stdout"
  ln -s /proc/self/fd/2 "${DYMAL_MOUNT_ROOT}/dev/stderr"

  ip link set lo up

  assert_mount_option "${DYMAL_MOUNT_ROOT}/opt/python" ro
  assert_mount_option "${DYMAL_MOUNT_ROOT}/opt/skillsandbox" ro
  assert_mount_option "${DYMAL_MOUNT_ROOT}/opt/evaluator/stage7_v3_pipeline.py" ro
  assert_mount_option "${DYMAL_MOUNT_ROOT}/opt/evaluator/stage7_v3_behavior_verifier.py" ro
  assert_mount_option "${DYMAL_MOUNT_ROOT}/opt/verifier_12" ro
  assert_mount_option "${DYMAL_MOUNT_ROOT}/input/dataset" ro
  assert_mount_option "${DYMAL_MOUNT_ROOT}/input/task_prompts.json" ro
  assert_mount_option "${DYMAL_MOUNT_ROOT}/output/staging" rw

  mount -o remount,ro,nosuid,nodev tmpfs "${DYMAL_MOUNT_ROOT}"
  assert_mount_option "${DYMAL_MOUNT_ROOT}" ro

  ulimit -c 0
  ulimit -f 2097152
  ulimit -n 4096
  ulimit -u 128

  local d3_required=0
  local d3_control_dir="${DYMAL_MOUNT_ROOT}/output/staging/.d3-control"
  local d3_expected_relative=""
  local d3_nonce=""
  if [[ "${DYMAL_DEFENSE}" =~ ^(d3|d1\+d3)$ \
    && "${DYMAL_EXECUTION_MODE}" =~ ^(pipeline|d3-probe)$ ]]; then
    d3_required=1
    d3_expected_relative="output/${DYMAL_ATTACK}/${DYMAL_SKILL_ID}/workspace/.agents/skills/${DYMAL_SKILL_ID}"
    d3_nonce="$(< /proc/sys/kernel/random/uuid)"
    mkdir -m 0700 -- "${d3_control_dir}"
  fi

  chroot "${DYMAL_MOUNT_ROOT}" \
    /usr/bin/setpriv \
      --bounding-set=-all \
      --inh-caps=-all \
      --ambient-caps=-all \
      --nnp \
      /usr/bin/env -i \
        BROWSER=/bin/false \
        CUDA_VISIBLE_DEVICES= \
        DYMAL_ATTACK="${DYMAL_ATTACK}" \
        DYMAL_ATTEMPT="${DYMAL_ATTEMPT}" \
        DYMAL_DEFENSE="${DYMAL_DEFENSE}" \
        DYMAL_D3_CONTROL_DIR=/output/staging/.d3-control \
        DYMAL_D3_EXPECTED_RELATIVE="${d3_expected_relative}" \
        DYMAL_D3_HANDSHAKE_TIMEOUT_SECONDS=30 \
        DYMAL_D3_NONCE="${d3_nonce}" \
        DYMAL_EXECUTION_MODE="${DYMAL_EXECUTION_MODE}" \
        DYMAL_TEMPERATURE="${DYMAL_TEMPERATURE}" \
        DYMAL_GENERATION_SEED="${DYMAL_GENERATION_SEED}" \
        DYMAL_MAX_OUTPUT_TOKENS="${DYMAL_MAX_OUTPUT_TOKENS}" \
        DYMAL_NUM_RETRIES="${DYMAL_NUM_RETRIES}" \
        DYMAL_INTERNAL_MARKER=runner-v1 \
        DYMAL_MODEL="${DYMAL_MODEL}" \
        DYMAL_SANDBOX_PORT="${DYMAL_SANDBOX_PORT}" \
        DYMAL_SKILLSANDBOX_MOUNT=/opt/skillsandbox \
        DYMAL_SKILL_ID="${DYMAL_SKILL_ID}" \
        DYMAL_TEST_COMMAND="${DYMAL_TEST_COMMAND}" \
        DYMAL_TRIAL_TIMEOUT_SECONDS="${DYMAL_TRIAL_TIMEOUT_SECONDS}" \
        DYMAL_VERIFIER_ROOT=/opt/verifier_12 \
        DYMAL_VERIFIER_TIMEOUT_SECONDS=8 \
        HF_HUB_OFFLINE=1 \
        HOME=/home/sandbox \
        LANG=C.UTF-8 \
        LC_ALL=C.UTF-8 \
        LITELLM_LOCAL_MODEL_COST_MAP=True \
        NVIDIA_VISIBLE_DEVICES=void \
        OPENAI_API_KEY=dummy \
        OPENHANDS_SUPPRESS_BANNER=1 \
        PATH=/opt/shims:/opt/python/bin:/usr/local/bin:/usr/bin:/bin \
        PYTHONDONTWRITEBYTECODE=1 \
        TMPDIR=/tmp \
        TRANSFORMERS_OFFLINE=1 \
        XDG_CACHE_HOME=/tmp/.cache \
        /bin/bash /opt/evaluator/run_isolated_dymalskill_trial.sh --_runner &
  local runner_pid=$!
  local d3_mounted=0
  local d3_controller_error=""
  local d3_target="${DYMAL_MOUNT_ROOT}/output/staging/${d3_expected_relative}"
  local request_path="${d3_control_dir}/request.json"
  local ack_path="${d3_control_dir}/ack.json"

  while kill -0 "${runner_pid}" 2>/dev/null; do
    if (( d3_required == 1 && d3_mounted == 0 )) && [[ -e "${request_path}" ]]; then
      if ! validate_d3_mount_request \
        "${request_path}" "${d3_nonce}" "${d3_expected_relative}"; then
        d3_controller_error="invalid D3 mount request"
        kill -TERM "${runner_pid}" 2>/dev/null || true
        break
      fi
      if [[ -L "${d3_target}" || ! -d "${d3_target}" \
        || "$(realpath -e -- "${d3_target}")" != "${d3_target}" ]]; then
        d3_controller_error="D3 target path is missing, symlinked, or non-canonical"
        kill -TERM "${runner_pid}" 2>/dev/null || true
        break
      fi
      if ! mount --bind -- "${d3_target}" "${d3_target}" \
        || ! mount -o remount,bind,ro,nosuid,nodev -- "${d3_target}"; then
        d3_controller_error="D3 bind-remount failed"
        kill -TERM "${runner_pid}" 2>/dev/null || true
        break
      fi
      d3_mounted=1
      if ! assert_mount_option "${d3_target}" ro; then
        d3_controller_error="D3 mount lacks read-only option"
        kill -TERM "${runner_pid}" 2>/dev/null || true
        break
      fi
      local d3_mount_options
      d3_mount_options="$(findmnt -n -o OPTIONS --target "${d3_target}")"
      if ! write_d3_mount_ack \
        "${ack_path}" "${d3_nonce}" "${d3_expected_relative}" "${d3_mount_options}"; then
        d3_controller_error="D3 acknowledgement publication failed"
        kill -TERM "${runner_pid}" 2>/dev/null || true
        break
      fi
    fi
    sleep 0.025
  done

  local runner_rc=0
  wait "${runner_pid}" || runner_rc=$?
  if (( d3_mounted == 1 )); then
    umount -- "${d3_target}" || {
      printf 'failed to unmount D3 skill tree: %s\n' "${d3_target}" >&2
      runner_rc=74
    }
  fi
  if [[ -n "${d3_controller_error}" ]]; then
    printf '%s\n' "${d3_controller_error}" >&2
    return 74
  fi
  if (( d3_required == 1 && d3_mounted == 0 )); then
    printf 'D3 runner exited without establishing a read-only mount\n' >&2
    return 74
  fi
  return "${runner_rc}"
}

if [[ "${1:-}" == "--_namespace" ]]; then
  shift
  (( $# == 0 )) || die "unexpected namespace arguments"
  run_inside_namespace
  exit $?
fi

usage() {
  cat >&2 <<'EOF'
Usage: run_isolated_dymalskill_trial.sh \
  --attack NAME --defense {none,d1,d3,d1+d3} --skill-id ID \
  --prompt-manifest FILE --dataset-root DIR --staging-dir DIR \
  --host-unix-socket SOCKET --attempt N [options]

Options:
  --staging-root DIR          Alias for --staging-dir.
  --python-env DIR            Read-only Python environment.
  --skillsandbox-root DIR     Read-only SkillSandbox source tree.
  --model NAME                Model identifier sent to the local endpoint.
  --temperature N             Must be 0 for this frozen smoke test.
  --seed N                    Must be 0 for this frozen smoke test.
  --max-output-tokens N       Must be 4096 for this frozen smoke test.
  --num-retries N             Must be 1 for this frozen smoke test.
  --trial-timeout-seconds N   Pipeline timeout (default: 600).
  --sandbox-port N            Namespace-local bridge port (default: 18100).
  --probe                     Run the built-in executable isolation probe.
  --d3-probe                  TEST ONLY: exercise the real D3 mount and mutation probes.
  --test-command COMMAND      TEST ONLY: run COMMAND instead of pipeline.
EOF
}

ATTACK=""
DEFENSE=""
SKILL_ID=""
PROMPT_MANIFEST=""
DATASET_ROOT=""
STAGING_DIR=""
HOST_UNIX_SOCKET=""
ATTEMPT=""
PYTHON_ENV="/work/tc442/miniconda3/envs/qwen300"
SKILLSANDBOX_ROOT="${EXPERIMENT_DIR}/vendor/skillsandbox_pipeline"
MODEL="openai//work/tc442/models/Qwen3.5-9B"
TRIAL_TIMEOUT_SECONDS=1200
SANDBOX_PORT=18100
TEMPERATURE=0
GENERATION_SEED=0
MAX_OUTPUT_TOKENS=4096
NUM_RETRIES=1
EXECUTION_MODE=pipeline
TEST_COMMAND=""

while (( $# > 0 )); do
  case "$1" in
    --attack) ATTACK="${2:-}"; shift 2 ;;
    --defense) DEFENSE="${2:-}"; shift 2 ;;
    --skill-id) SKILL_ID="${2:-}"; shift 2 ;;
    --prompt-manifest) PROMPT_MANIFEST="${2:-}"; shift 2 ;;
    --dataset-root) DATASET_ROOT="${2:-}"; shift 2 ;;
    --staging-dir|--staging-root) STAGING_DIR="${2:-}"; shift 2 ;;
    --host-unix-socket) HOST_UNIX_SOCKET="${2:-}"; shift 2 ;;
    --attempt) ATTEMPT="${2:-}"; shift 2 ;;
    --python-env) PYTHON_ENV="${2:-}"; shift 2 ;;
    --skillsandbox-root) SKILLSANDBOX_ROOT="${2:-}"; shift 2 ;;
    --model) MODEL="${2:-}"; shift 2 ;;
    --temperature) TEMPERATURE="${2:-}"; shift 2 ;;
    --seed) GENERATION_SEED="${2:-}"; shift 2 ;;
    --max-output-tokens) MAX_OUTPUT_TOKENS="${2:-}"; shift 2 ;;
    --num-retries) NUM_RETRIES="${2:-}"; shift 2 ;;
    --trial-timeout-seconds) TRIAL_TIMEOUT_SECONDS="${2:-}"; shift 2 ;;
    --sandbox-port) SANDBOX_PORT="${2:-}"; shift 2 ;;
    --probe)
      [[ "${EXECUTION_MODE}" == pipeline ]] || die "choose only one test mode"
      EXECUTION_MODE=probe
      shift
      ;;
    --d3-probe)
      [[ "${EXECUTION_MODE}" == pipeline ]] || die "choose only one test mode"
      EXECUTION_MODE=d3-probe
      shift
      ;;
    --test-command)
      [[ "${EXECUTION_MODE}" == pipeline ]] || die "choose only one test mode"
      EXECUTION_MODE=test-command
      TEST_COMMAND="${2:-}"
      shift 2
      ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

readonly VALID_ATTACKS=' credential_abuse data_exfil_http data_exfil_file data_exfil_log mock_api dos rce db_insert file_delete db_delete cpu_hijack gpu_hijack '
[[ "${VALID_ATTACKS}" == *" ${ATTACK} "* ]] || die "invalid attack: ${ATTACK:-<empty>}"
[[ "${DEFENSE}" =~ ^(none|d1|d3|d1\+d3)$ ]] || die "invalid defense: ${DEFENSE:-<empty>}"
[[ "${SKILL_ID}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ && "${SKILL_ID}" != *..* ]] \
  || die "invalid skill id: ${SKILL_ID:-<empty>}"
[[ "${ATTEMPT}" =~ ^[1-9][0-9]*$ ]] || die "attempt must be a positive integer"
[[ "${TRIAL_TIMEOUT_SECONDS}" =~ ^[1-9][0-9]*$ ]] || die "trial timeout must be positive"
[[ "${TEMPERATURE}" == 0 || "${TEMPERATURE}" == 0.0 ]] \
  || die "temperature must be 0"
[[ "${GENERATION_SEED}" == 0 ]] || die "seed must be 0"
[[ "${MAX_OUTPUT_TOKENS}" == 4096 ]] || die "max output tokens must be 4096"
[[ "${NUM_RETRIES}" == 1 ]] || die "num retries must be 1"
[[ "${SANDBOX_PORT}" =~ ^[0-9]+$ ]] && (( SANDBOX_PORT >= 1024 && SANDBOX_PORT <= 65535 )) \
  || die "sandbox port must be in [1024, 65535]"
[[ -n "${DATASET_ROOT}" ]] || die "--dataset-root is required"
[[ -n "${HOST_UNIX_SOCKET}" ]] || die "--host-unix-socket is required"
[[ -n "${STAGING_DIR}" ]] || die "--staging-dir is required"
[[ "${EXECUTION_MODE}" != test-command || -n "${TEST_COMMAND}" ]] || die "empty test command"
[[ "${EXECUTION_MODE}" != d3-probe || "${DEFENSE}" =~ ^(d3|d1\+d3)$ ]] \
  || die "--d3-probe requires defense d3 or d1+d3"

[[ ! -L "${PROMPT_MANIFEST}" && -f "${PROMPT_MANIFEST}" ]] || die "prompt manifest must be a regular non-symlink file"
[[ ! -L "${DATASET_ROOT}" && -d "${DATASET_ROOT}" ]] || die "dataset root must be a non-symlink directory"
[[ ! -L "${STAGING_DIR}" && -d "${STAGING_DIR}" ]] || die "staging directory must be a non-symlink directory"
[[ ! -L "${PYTHON_ENV}" && -x "${PYTHON_ENV}/bin/python" ]] || die "invalid Python environment"
[[ ! -L "${SKILLSANDBOX_ROOT}" && -f "${SKILLSANDBOX_ROOT}/pipeline.py" ]] || die "invalid SkillSandbox root"
[[ -S "${HOST_UNIX_SOCKET}" ]] || die "host model socket is missing or not a Unix socket"
[[ -f "${SUPPORT_DIR}/model_socket_bridge.py" ]] || die "model socket bridge is missing"
[[ -x "${SUPPORT_DIR}/dymalskill_blocked_command.sh" ]] || die "blocked-command shim is not executable"
[[ -f "${V3_PIPELINE_HOST}" && ! -L "${V3_PIPELINE_HOST}" ]] \
  || die "Stage 7 v3 pipeline adapter is missing or symlinked"
[[ -f "${V3_BEHAVIOR_VERIFIER_HOST}" && ! -L "${V3_BEHAVIOR_VERIFIER_HOST}" ]] \
  || die "Stage 7 v3 behavior verifier adapter is missing or symlinked"
[[ -d "${V3_VERIFIER_BUNDLE_HOST}" && ! -L "${V3_VERIFIER_BUNDLE_HOST}" \
  && -f "${V3_VERIFIER_BUNDLE_HOST}/MANIFEST.json" ]] \
  || die "frozen SkillSandbox verifier bundle is missing or symlinked"

PROMPT_MANIFEST="$(realpath -e -- "${PROMPT_MANIFEST}")"
DATASET_ROOT="$(realpath -e -- "${DATASET_ROOT}")"
STAGING_DIR="$(realpath -e -- "${STAGING_DIR}")"
PYTHON_ENV="$(realpath -e -- "${PYTHON_ENV}")"
SKILLSANDBOX_ROOT="$(realpath -e -- "${SKILLSANDBOX_ROOT}")"
HOST_UNIX_SOCKET="$(realpath -e -- "${HOST_UNIX_SOCKET}")"
V3_PIPELINE="$(realpath -e -- "${V3_PIPELINE_HOST}")"
V3_BEHAVIOR_VERIFIER="$(realpath -e -- "${V3_BEHAVIOR_VERIFIER_HOST}")"
V3_VERIFIER_BUNDLE="$(realpath -e -- "${V3_VERIFIER_BUNDLE_HOST}")"

for protected_root in \
  "${DATASET_ROOT}" "${PYTHON_ENV}" "${SKILLSANDBOX_ROOT}" \
  "${V3_PIPELINE}" "${V3_BEHAVIOR_VERIFIER}" "${V3_VERIFIER_BUNDLE}"; do
  [[ "${STAGING_DIR}/" != "${protected_root}/"* && "${protected_root}/" != "${STAGING_DIR}/"* ]] \
    || die "staging and protected source paths overlap: ${STAGING_DIR} ${protected_root}"
done

[[ -d "${DATASET_ROOT}/${ATTACK}/${SKILL_ID}" ]] \
  || die "assigned dataset skill does not exist: ${ATTACK}/${SKILL_ID}"
[[ ! -e "${STAGING_DIR}/output" ]] \
  || die "staging output already exists: ${STAGING_DIR}/output"
for runner_log in runner.stdout.log runner.stderr.log; do
  [[ ! -e "${STAGING_DIR}/${runner_log}" || ( -f "${STAGING_DIR}/${runner_log}" && ! -L "${STAGING_DIR}/${runner_log}" ) ]] \
    || die "invalid runner log path in staging: ${STAGING_DIR}/${runner_log}"
done
unexpected_staging_entry="$(
  find "${STAGING_DIR}" -mindepth 1 -maxdepth 1 \
    ! -name runner.stdout.log ! -name runner.stderr.log -print -quit
)"
[[ -z "${unexpected_staging_entry}" ]] \
  || die "staging contains an unexpected entry: ${unexpected_staging_entry}"

/usr/bin/python3 - "${PROMPT_MANIFEST}" "${ATTACK}" "${SKILL_ID}" <<'PY'
import json
import sys

manifest_path, expected_attack, skill_id = sys.argv[1:]
with open(manifest_path, encoding="utf-8") as stream:
    payload = json.load(stream)
if payload.get("attack") not in (None, expected_attack):
    raise SystemExit("prompt manifest attack does not match assignment")
records = payload.get("skills")
matches = [item for item in records or [] if item.get("skill_id") == skill_id]
if len(matches) != 1:
    raise SystemExit(f"prompt manifest must contain assigned skill exactly once: {skill_id}")
PY

MOUNT_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/dymalskill-isolated-root.XXXXXX")"
cleanup_mount_root() {
  rmdir -- "${MOUNT_ROOT}" 2>/dev/null || true
}
trap cleanup_mount_root EXIT

env -i \
  DYMAL_ATTACK="${ATTACK}" \
  DYMAL_ATTEMPT="${ATTEMPT}" \
  DYMAL_BLOCKED_SCRIPT="${SUPPORT_DIR}/dymalskill_blocked_command.sh" \
  DYMAL_BRIDGE_SCRIPT="${SUPPORT_DIR}/model_socket_bridge.py" \
  DYMAL_DATASET_ROOT="${DATASET_ROOT}" \
  DYMAL_DEFENSE="${DEFENSE}" \
  DYMAL_EXECUTION_MODE="${EXECUTION_MODE}" \
  DYMAL_TEMPERATURE="${TEMPERATURE}" \
  DYMAL_GENERATION_SEED="${GENERATION_SEED}" \
  DYMAL_MAX_OUTPUT_TOKENS="${MAX_OUTPUT_TOKENS}" \
  DYMAL_NUM_RETRIES="${NUM_RETRIES}" \
  DYMAL_HOST_UNIX_SOCKET="${HOST_UNIX_SOCKET}" \
  DYMAL_INTERNAL_MARKER=namespace-v1 \
  DYMAL_MODEL="${MODEL}" \
  DYMAL_MOUNT_ROOT="${MOUNT_ROOT}" \
  DYMAL_PROMPT_MANIFEST="${PROMPT_MANIFEST}" \
  DYMAL_PYTHON_ENV="${PYTHON_ENV}" \
  DYMAL_SANDBOX_PORT="${SANDBOX_PORT}" \
  DYMAL_SCRIPT_HOST="${SCRIPT_PATH}" \
  DYMAL_SKILLSANDBOX_ROOT="${SKILLSANDBOX_ROOT}" \
  DYMAL_SKILL_ID="${SKILL_ID}" \
  DYMAL_STAGING_DIR="${STAGING_DIR}" \
  DYMAL_TEST_COMMAND="${TEST_COMMAND}" \
  DYMAL_TRIAL_TIMEOUT_SECONDS="${TRIAL_TIMEOUT_SECONDS}" \
  DYMAL_V3_BEHAVIOR_VERIFIER_HOST="${V3_BEHAVIOR_VERIFIER}" \
  DYMAL_V3_PIPELINE_HOST="${V3_PIPELINE}" \
  DYMAL_V3_VERIFIER_BUNDLE_HOST="${V3_VERIFIER_BUNDLE}" \
  PATH=/usr/sbin:/usr/bin:/sbin:/bin \
  /usr/bin/unshare \
    --user --map-root-user \
    --mount --net --pid --fork \
    --kill-child=TERM --mount-proc \
    /bin/bash "${SCRIPT_PATH}" --_namespace
