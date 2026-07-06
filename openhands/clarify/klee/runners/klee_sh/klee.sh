#!/usr/bin/env bash
# =============================================================================
# klee.sh — KLEE Docker 环境一键准备 + 长生存期容器 + 求解 / 重放入口
#
# 适用场景：在新机器（仅装了 Docker）上准备 KLEE 容器，对任意 .cpp 跑符号执行，
#           并按需对每条路径做 replay，落出宿主可解析的中间 JSON。
#
# 设计要点：
#   - 长生存期容器（docker run -d ... sleep infinity），靠 docker cp 推/拉文件。
#   - 不依赖 bind mount，避免宿主权限/SELinux 等坑。
#   - 容器内有两个工具脚本：
#       * solve_workspace.sh   把 ws 编译成 bitcode 并跑 KLEE
#       * replay_workspace.sh  用 -lkleeRuntest 编译 replay 二进制，逐条 ktest 重放
#     默认 start 时 docker cp 进容器；也可 ./klee.sh build-image 烘焙到镜像里。
#   - 容器只产 KLEE 原始结果 + replay 原始 JSONL，宿主侧再做业务 schema 翻译。
#   - 全部参数通过命令行传入，不依赖环境变量。
#
# 用法：
#   ./klee.sh [全局参数] <子命令> [子命令参数]
#
# 子命令：
#   prepare              检查 docker、拉镜像、端到端冒烟自测
#   start                创建并启动长生存期容器（含 solver/replay 安装）
#   stop                 停止容器
#   rm                   删除容器
#   status               查看容器状态
#   shell                进入容器交互
#   build-image          把 solver/replay 烘焙进镜像
#   solve <src> [out]    src = .cpp 文件 或 含若干 .cpp 的目录
#                        out = 主机端 klee-out 输出目录，默认 ./klee-out-last
#   replay <src> <klee_out_dir> [host_replay_out]
#                        用 ktest 重放，逐条产生 replay/testNNNNNN.json
#                        host_replay_out 默认为 <klee_out_dir>/../replay
#   solve-and-replay <src> <out_root>
#                        一把梭：<out_root>/klee-out/ + <out_root>/replay/
#   dump-layout <ws_dir> <host_out_json>
#                        在容器内 clang++ 编译 <ws_dir>/dump_layout.cpp，跑出
#                        layout_manifest.json（含 sizeof/offsetof + klee::Repeated
#                        wrapper 内部 layout）。供 KLEE replay → ut/mock JSON 解码用。
#   selftest             用 example_symbolic_main.cpp 做端到端自测
#
# 全局参数（任何子命令前可传）：
#   --image <img>                 KLEE 镜像；默认按当前系统平台选择 klee/klee:latest
#   --platform <p>                Docker 平台，默认 auto（当前系统）；
#                                 支持 auto|native|linux/amd64|linux/arm64|amd64|arm64
#   --container-name <name>       容器名，默认 klee-solver-stable
#   --solver-image <tag>          build-image 输出 tag，默认 klee/solver-with-workspace:latest
#   --solver-container-path <p>   容器内 solve 脚本路径；不设则自动探测
#   --replay-container-path <p>   容器内 replay 脚本路径；不设则自动探测
#   --skip-pull                   prepare/auto-pull 时不联网
#
# solve / replay / solve-and-replay 共享参数：
#   --max-seconds <N>             KLEE 求解超时（仅 solve），默认 120
#   --max-forks <N>               KLEE 路径上限（仅 solve），默认 50000
#   --only-output-states-covering-new
#                                 仅输出覆盖新指令的 KLEE 状态（仅 solve）
#   --cpp-std <STD>               clang++ 标准，默认 -std=c++17
#   --cpp-flags <"FLAGS">         clang++ 额外参数（可重复传，将拼接）
#   --replay-parallel <N>         replay 并行度（仅 replay/solve-and-replay），默认 1
#   --replay-list <host_path>     仅重放给定 testNNNNNN 列表的 ktest（每行一个 id 或
#                                 testNNNNNN.ktest，#/空行忽略）。文件会被 docker cp
#                                 进容器后传给 replay_workspace.sh。
#
# 离线机：可联网机
#     docker pull klee/klee:latest && docker save klee/klee:latest -o klee.tar
#   目标机
#     docker load -i klee.tar
#     ./klee.sh --skip-pull prepare
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

# -------- 默认参数 -----------------------------------------------------------

IMG=""
DOCKER_PLATFORM="auto"
NAME="klee-solver-stable"
SOLVER_TAG="klee/solver-with-workspace:latest"
SOLVER_CONTAINER_PATH=""
REPLAY_CONTAINER_PATH=""
SKIP_PULL=0

# 共享 solve / replay 参数
MAX_SECONDS="120"
MAX_FORKS="50000"
ONLY_OUTPUT_STATES_COVERING_NEW=""
CPP_STD="-std=c++17"
CPP_FLAGS=""

# solve 搜索/内存调优（缺省为空 = 透传给容器脚本时不附加，沿用 KLEE 默认）。
# 由 tools/klee_solve.py 依据 klee_config.yaml 显式传入。
SEARCH=()
USE_BATCHING_SEARCH=""
BATCH_INSTRUCTIONS=""
MAX_MEMORY=""

# replay 专属（solve 阶段忽略）
REPLAY_PARALLEL=""
REPLAY_LIST=""

SOLVER_SRC="${ROOT}/klee_interface/solve_workspace.sh"
REPLAY_SRC="${ROOT}/klee_interface/replay_workspace.sh"
# 容器内候选路径（先看 build-image 烘焙的，再看 docker cp 安装的）
SOLVER_BAKED_DEFAULT="/usr/local/bin/klee_solve_workspace.sh"
REPLAY_BAKED_DEFAULT="/usr/local/bin/klee_replay_workspace.sh"
SOLVER_INSTALLED="/opt/klee_solver/solve_workspace.sh"
REPLAY_INSTALLED="/opt/klee_solver/replay_workspace.sh"
EXAMPLE_CPP="${ROOT}/klee_interface/example_symbolic_main.cpp"

die()  { echo "ERROR: $*" >&2; exit 1; }
info() { echo "==> $*"; }

# -------- 参数解析 -----------------------------------------------------------

# 全局参数（出现在子命令前；solve/replay/solve-and-replay 也允许穿插）
parse_global_arg() {
  case "$1" in
    --image)                   IMG="$2"; return 2 ;;
    --image=*)                 IMG="${1#*=}"; return 1 ;;
    --platform)                DOCKER_PLATFORM="$2"; return 2 ;;
    --platform=*)              DOCKER_PLATFORM="${1#*=}"; return 1 ;;
    --container-name)          NAME="$2"; return 2 ;;
    --container-name=*)        NAME="${1#*=}"; return 1 ;;
    --solver-image)            SOLVER_TAG="$2"; return 2 ;;
    --solver-image=*)          SOLVER_TAG="${1#*=}"; return 1 ;;
    --solver-container-path)   SOLVER_CONTAINER_PATH="$2"; return 2 ;;
    --solver-container-path=*) SOLVER_CONTAINER_PATH="${1#*=}"; return 1 ;;
    --replay-container-path)   REPLAY_CONTAINER_PATH="$2"; return 2 ;;
    --replay-container-path=*) REPLAY_CONTAINER_PATH="${1#*=}"; return 1 ;;
    --skip-pull)               SKIP_PULL=1; return 1 ;;
    *) return 0 ;;
  esac
}

# solve / replay / solve-and-replay 公用参数
parse_run_arg() {
  case "$1" in
    --max-seconds)   MAX_SECONDS="$2"; return 2 ;;
    --max-seconds=*) MAX_SECONDS="${1#*=}"; return 1 ;;
    --max-forks)     MAX_FORKS="$2"; return 2 ;;
    --max-forks=*)   MAX_FORKS="${1#*=}"; return 1 ;;
    --only-output-states-covering-new)
      ONLY_OUTPUT_STATES_COVERING_NEW=1; return 1 ;;
    --search)        SEARCH+=("$2"); return 2 ;;
    --search=*)      SEARCH+=("${1#*=}"); return 1 ;;
    --use-batching-search)
      USE_BATCHING_SEARCH=1; return 1 ;;
    --batch-instructions)   BATCH_INSTRUCTIONS="$2"; return 2 ;;
    --batch-instructions=*) BATCH_INSTRUCTIONS="${1#*=}"; return 1 ;;
    --max-memory)    MAX_MEMORY="$2"; return 2 ;;
    --max-memory=*)  MAX_MEMORY="${1#*=}"; return 1 ;;
    --cpp-std)       CPP_STD="$2"; return 2 ;;
    --cpp-std=*)     CPP_STD="${1#*=}"; return 1 ;;
    --cpp-flags)     CPP_FLAGS="${CPP_FLAGS:+$CPP_FLAGS }$2"; return 2 ;;
    --cpp-flags=*)   CPP_FLAGS="${CPP_FLAGS:+$CPP_FLAGS }${1#*=}"; return 1 ;;
    --replay-parallel)   REPLAY_PARALLEL="$2"; return 2 ;;
    --replay-parallel=*) REPLAY_PARALLEL="${1#*=}"; return 1 ;;
    --replay-list)       REPLAY_LIST="$2"; return 2 ;;
    --replay-list=*)     REPLAY_LIST="${1#*=}"; return 1 ;;
    *) return 0 ;;
  esac
}

# 先吃掉子命令前的全局参数
while [[ $# -gt 0 ]]; do
  rc=0
  parse_global_arg "$@" || rc=$?
  if [[ $rc -eq 0 ]]; then break; fi
  shift "$rc"
done

# -------- docker / 镜像 / 容器辅助 -------------------------------------------

check_docker() {
  command -v docker >/dev/null 2>&1 \
    || die "未找到 docker。请先安装 Docker Engine: https://docs.docker.com/engine/install/"
  docker info >/dev/null 2>&1 \
    || die "Docker 未运行或当前用户无权限。请启动 docker 服务，并 sudo usermod -aG docker \$USER（重新登录后生效）。"
}

normalize_arch() {
  case "$1" in
    amd64|x86_64) echo "amd64" ;;
    arm64|aarch64) echo "arm64" ;;
    *) die "不支持的 CPU 架构: $1（请显式传 --platform linux/amd64 或 linux/arm64）" ;;
  esac
}

detect_system_platform() {
  local os arch
  os="$(docker info --format '{{.OSType}}' 2>/dev/null || true)"
  arch="$(docker info --format '{{.Architecture}}' 2>/dev/null || true)"
  if [[ -z "${os}" || -z "${arch}" ]]; then
    os="linux"
    arch="$(uname -m)"
  fi

  # Docker 镜像平台是 linux/*；macOS/Windows Docker Desktop 也运行 Linux VM。
  if [[ "${os}" != "linux" ]]; then
    os="linux"
  fi
  echo "${os}/$(normalize_arch "${arch}")"
}

resolved_platform() {
  case "${DOCKER_PLATFORM}" in
    ""|none)
      echo ""
      ;;
    auto|native)
      detect_system_platform
      ;;
    linux/amd64|linux/arm64)
      echo "${DOCKER_PLATFORM}"
      ;;
    amd64|x86_64)
      echo "linux/amd64"
      ;;
    arm64|aarch64)
      echo "linux/arm64"
      ;;
    *)
      die "不支持的 --platform=${DOCKER_PLATFORM}；支持 auto|native|linux/amd64|linux/arm64|amd64|arm64"
      ;;
  esac
}

default_image_for_platform() {
  case "$1" in
    linux/amd64|linux/arm64|"")
      echo "klee/klee:latest"
      ;;
    *)
      die "不支持的平台: $1"
      ;;
  esac
}

resolve_image() {
  local platform="$1"
  if [[ -z "${IMG}" ]]; then
    IMG="$(default_image_for_platform "${platform}")"
  fi
}

local_image_platform() {
  docker image inspect "${IMG}" --format '{{.Os}}/{{.Architecture}}' 2>/dev/null || true
}

effective_platform_for_image() {
  local platform image_platform
  platform="$(resolved_platform)"
  resolve_image "${platform}"
  image_platform="$(local_image_platform)"

  if [[ "${DOCKER_PLATFORM}" == "auto" || "${DOCKER_PLATFORM}" == "native" ]]; then
    if [[ -n "${image_platform}" && -n "${platform}" && "${image_platform}" != "${platform}" ]]; then
      echo "${image_platform}"
      return 0
    fi
  fi

  echo "${platform}"
}

ensure_image_present() {
  local platform image_platform
  platform="$(effective_platform_for_image)"
  resolve_image "${platform}"

  image_platform="$(local_image_platform)"
  if [[ -n "${image_platform}" && ( -z "${platform}" || "${image_platform}" == "${platform}" ) ]]; then
    local native_platform
    native_platform="$(resolved_platform)"
    if [[ "${DOCKER_PLATFORM}" == "auto" || "${DOCKER_PLATFORM}" == "native" ]]; then
      if [[ -n "${native_platform}" && "${image_platform}" != "${native_platform}" ]]; then
        echo "WARN: 本机镜像 ${IMG} 是 ${image_platform}，当前系统是 ${native_platform}；将继续使用已有镜像平台。" >&2
      fi
    fi
    return 0
  fi
  if [[ "${SKIP_PULL}" == "1" ]]; then
    if [[ -n "${image_platform}" && -n "${platform}" && "${image_platform}" != "${platform}" ]]; then
      die "本机镜像 ${IMG} 是 ${image_platform}，当前需要 ${platform}，且 --skip-pull 已开启。请加载对应平台镜像或改用 --platform。"
    fi
    die "本机没有镜像 ${IMG}，且 --skip-pull 已开启。请先 docker load 或不传 --skip-pull。"
  fi

  local platform_args=()
  if [[ -n "${platform}" ]]; then
    platform_args=(--platform "${platform}")
  fi
  info "docker pull ${platform_args[*]} ${IMG}"
  docker pull "${platform_args[@]}" "${IMG}"
}

container_image_platform() {
  local image_id
  image_id="$(docker inspect -f '{{.Image}}' "${NAME}" 2>/dev/null || true)"
  [[ -n "${image_id}" ]] || return 0
  docker image inspect "${image_id}" --format '{{.Os}}/{{.Architecture}}' 2>/dev/null || true
}

# 容器内某工具脚本路径（solve|replay）。返回容器绝对路径或空字符串。
tool_path_in_container() {
  case "$1" in
    solve)
      [[ -n "${SOLVER_CONTAINER_PATH}" ]] && { echo "${SOLVER_CONTAINER_PATH}"; return 0; }
      docker exec "${NAME}" test -x "${SOLVER_BAKED_DEFAULT}" 2>/dev/null && { echo "${SOLVER_BAKED_DEFAULT}"; return 0; }
      docker exec "${NAME}" test -x "${SOLVER_INSTALLED}"     2>/dev/null && { echo "${SOLVER_INSTALLED}";     return 0; }
      ;;
    replay)
      [[ -n "${REPLAY_CONTAINER_PATH}" ]] && { echo "${REPLAY_CONTAINER_PATH}"; return 0; }
      docker exec "${NAME}" test -x "${REPLAY_BAKED_DEFAULT}" 2>/dev/null && { echo "${REPLAY_BAKED_DEFAULT}"; return 0; }
      docker exec "${NAME}" test -x "${REPLAY_INSTALLED}"     2>/dev/null && { echo "${REPLAY_INSTALLED}";     return 0; }
      ;;
  esac
  echo ""
}

install_tool_into_container() {
  # $1: tool name (solve|replay), $2: host path, $3: container path
  local tool="$1" src="$2" dst="$3"
  [[ -f "${src}" ]] || die "缺少 ${src}（容器内 ${tool} 源文件）"
  info "安装 ${tool} 到 ${NAME}:${dst}"
  docker exec -u 0 "${NAME}" mkdir -p "$(dirname "${dst}")" \
    || die "容器内 mkdir 失败（docker exec -u 0）。可改用 ./klee.sh build-image"
  docker cp "${src}" "${NAME}:${dst}"
  docker exec -u 0 "${NAME}" chmod 755 "${dst}"
  docker exec -u 0 "${NAME}" chown klee:klee "${dst}" 2>/dev/null || true
}

ensure_container_ready() {
  check_docker
  ensure_image_present
  local platform
  platform="$(effective_platform_for_image)"
  resolve_image "${platform}"

  if ! docker inspect "${NAME}" >/dev/null 2>&1; then
    local platform_args=()
    if [[ -n "${platform}" ]]; then
      platform_args=(--platform "${platform}")
    fi
    info "创建容器 ${NAME} (image=${IMG}, platform=${platform:-docker-default})"
    docker run "${platform_args[@]}" -d --restart unless-stopped --ulimit='stack=-1:-1' \
      --name "${NAME}" "${IMG}" sleep infinity >/dev/null
  elif [[ -n "${platform}" ]]; then
    local existing_platform
    existing_platform="$(container_image_platform)"
    if [[ -n "${existing_platform}" && "${existing_platform}" != "${platform}" ]]; then
      echo "WARN: 已有容器 ${NAME} 使用 ${existing_platform} 镜像，目标平台是 ${platform}；继续复用现有容器。" >&2
    fi
  fi

  local st; st="$(docker inspect -f '{{.State.Status}}' "${NAME}")"
  if [[ "${st}" != "running" ]]; then
    info "启动 ${NAME} (was: ${st})"
    docker start "${NAME}" >/dev/null
  fi

  # solve 脚本：用户显式指定路径仅校验存在；否则**每次都从宿主重推**，
  # 避免容器残留旧版（例如缺少 -DKLEE_SOLVE=1）造成 host 源码已修复但容器里
  # 还跑老逻辑的"幽灵 bug"。烘焙到镜像（SOLVER_BAKED_DEFAULT）的版本仍优先。
  if [[ -n "${SOLVER_CONTAINER_PATH}" ]]; then
    docker exec "${NAME}" test -x "${SOLVER_CONTAINER_PATH}" \
      || die "--solver-container-path=${SOLVER_CONTAINER_PATH} 在容器内不可执行"
  elif docker exec "${NAME}" test -x "${SOLVER_BAKED_DEFAULT}" 2>/dev/null; then
    : # 已烘焙到镜像，按 build-image 流程使用
  else
    install_tool_into_container "solve" "${SOLVER_SRC}" "${SOLVER_INSTALLED}"
  fi

  # replay 脚本（同上）
  if [[ -n "${REPLAY_CONTAINER_PATH}" ]]; then
    docker exec "${NAME}" test -x "${REPLAY_CONTAINER_PATH}" \
      || die "--replay-container-path=${REPLAY_CONTAINER_PATH} 在容器内不可执行"
  elif docker exec "${NAME}" test -x "${REPLAY_BAKED_DEFAULT}" 2>/dev/null; then
    : # 已烘焙到镜像
  else
    install_tool_into_container "replay" "${REPLAY_SRC}" "${REPLAY_INSTALLED}"
  fi
}

# -------- 子命令实现 ---------------------------------------------------------

cmd_prepare() {
  info "[1/3] 检查 docker"
  check_docker
  echo "    docker: $(docker --version)"
  local platform
  platform="$(effective_platform_for_image)"
  resolve_image "${platform}"
  echo "    platform: ${platform:-docker-default}"

  info "[2/3] 准备镜像 ${IMG}"
  ensure_image_present
  local platform_args=()
  if [[ -n "${platform}" ]]; then
    platform_args=(--platform "${platform}")
  fi
  docker run "${platform_args[@]}" --rm --ulimit='stack=-1:-1' "${IMG}" bash -lc \
    'command -v klee >/dev/null && klee --version | head -1; \
     command -v clang++ >/dev/null && clang++ --version | head -1' \
    || die "镜像 ${IMG} 内未找到 klee/clang++"

  info "[3/3] 端到端冒烟（example_symbolic_main.cpp）"
  if [[ -f "${EXAMPLE_CPP}" ]]; then
    cmd_selftest
  else
    echo "    跳过自测：缺少 ${EXAMPLE_CPP}"
  fi

  cat <<EOF

=== 环境就绪 ===
镜像        : ${IMG}
平台        : ${platform:-docker-default}
容器名      : ${NAME}（按需自动创建）
求解示例    : ./klee.sh solve /path/to/your.cpp
重放示例    : ./klee.sh replay <src> <klee_out_dir>
打开 shell  : ./klee.sh shell
EOF
}

cmd_start()  { ensure_container_ready; info "ready: ${NAME}（solver=$(tool_path_in_container solve) replay=$(tool_path_in_container replay)）"; }
cmd_stop()   { docker stop "${NAME}" >/dev/null 2>&1 || true; info "已停止 ${NAME}"; }
cmd_rm()     { docker rm -f "${NAME}" >/dev/null 2>&1 || true; info "已删除 ${NAME}"; }

cmd_status() {
  local native_platform platform
  native_platform="$(resolved_platform)"
  platform="$(effective_platform_for_image)"
  resolve_image "${platform}"
  echo "system platform: ${native_platform:-docker-default}"
  echo "target platform: ${platform:-docker-default}"
  echo "target image   : ${IMG}"
  if ! docker inspect "${NAME}" >/dev/null 2>&1; then
    echo "status: absent"; return 1
  fi
  echo "status: $(docker inspect -f '{{.State.Status}}' "${NAME}")"
  local existing_platform
  existing_platform="$(container_image_platform)"
  echo "image platform: ${existing_platform}"
  if [[ -n "${platform}" && -n "${existing_platform}" && "${existing_platform}" != "${platform}" ]]; then
    echo "WARN: container platform mismatch; continuing may use emulation"
  fi
  docker ps -a --filter "name=${NAME}" --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}' || true
}

cmd_shell()  { ensure_container_ready; docker exec -it "${NAME}" bash -l; }

cmd_build_image() {
  check_docker
  [[ -f "${ROOT}/Dockerfile.klee" ]] || die "缺少 ${ROOT}/Dockerfile.klee"
  [[ -f "${SOLVER_SRC}" ]]           || die "缺少 ${SOLVER_SRC}"
  [[ -f "${REPLAY_SRC}" ]]           || die "缺少 ${REPLAY_SRC}"
  local platform platform_args=()
  platform="$(resolved_platform)"
  if [[ -n "${platform}" ]]; then
    platform_args=(--platform "${platform}")
  fi
  info "docker build ${platform_args[*]} -t ${SOLVER_TAG}"
  docker build "${platform_args[@]}" -f "${ROOT}/Dockerfile.klee" -t "${SOLVER_TAG}" "$@" "${ROOT}"
  cat <<EOF
==> 已构建 ${SOLVER_TAG}
   切换为该镜像运行：
     ./klee.sh --image ${SOLVER_TAG} rm
     ./klee.sh --image ${SOLVER_TAG} start
EOF
}

# 把 src（文件或目录）拷进容器 ws；返回容器内 ws 路径。
push_src_to_container() {
  local src="$1" remote="$2"
  docker exec "${NAME}" mkdir -p "${remote}"
  if   [[ -f "${src}" ]]; then
    info "docker cp file -> ${NAME}:${remote}/"
    docker cp "${src}" "${NAME}:${remote}/"
  elif [[ -d "${src}" ]]; then
    info "docker cp dir  -> ${NAME}:${remote}/"
    docker cp "${src}/." "${NAME}:${remote}/"
  else
    die "不存在或类型未知: ${src}"
  fi
}

cmd_solve() {
  # 解析 solve 专属 + 全局（穿插）+ 收集位置参数
  local pos=()
  while [[ $# -gt 0 ]]; do
    local rc=0
    parse_run_arg "$@"    || rc=$?
    if [[ $rc -gt 0 ]]; then shift "$rc"; continue; fi
    parse_global_arg "$@" || rc=$?
    if [[ $rc -gt 0 ]]; then shift "$rc"; continue; fi
    pos+=("$1"); shift
  done

  local src="${pos[0]:-}"
  local out="${pos[1]:-${ROOT}/klee-out-last}"
  [[ -n "${src}" ]] || die "用法: $0 solve <file.cpp|dir> [host_output_dir] [--max-seconds N] [--max-forks N] [--only-output-states-covering-new] [--cpp-std STD] [--cpp-flags FLAGS]"

  ensure_container_ready
  local solver; solver="$(tool_path_in_container solve)"
  [[ -n "${solver}" ]] || die "容器内未发现 solve 脚本"

  local ts; ts="$(date +%s)_$$"
  local remote="/tmp/klee_ws_${ts}"
  push_src_to_container "${src}" "${remote}"

  info "在容器内执行 solver: ${solver} ${remote}"
  info "    --max-seconds=${MAX_SECONDS} --max-forks=${MAX_FORKS}${ONLY_OUTPUT_STATES_COVERING_NEW:+ --only-output-states-covering-new} --cpp-std=${CPP_STD}${CPP_FLAGS:+ --cpp-flags=\"${CPP_FLAGS}\"}"

  local cmd
  cmd="$(printf '%q' "${solver}")"
  cmd+=" --max-seconds $(printf '%q' "${MAX_SECONDS}")"
  cmd+=" --max-forks $(printf '%q' "${MAX_FORKS}")"
  if [[ -n "${ONLY_OUTPUT_STATES_COVERING_NEW}" ]]; then
    cmd+=" --only-output-states-covering-new"
  fi
  local _s
  if [[ ${#SEARCH[@]} -gt 0 ]]; then
    for _s in "${SEARCH[@]}"; do
      cmd+=" --search $(printf '%q' "${_s}")"
    done
  fi
  if [[ -n "${USE_BATCHING_SEARCH}" ]]; then
    cmd+=" --use-batching-search"
  fi
  if [[ -n "${BATCH_INSTRUCTIONS}" ]]; then
    cmd+=" --batch-instructions $(printf '%q' "${BATCH_INSTRUCTIONS}")"
  fi
  if [[ -n "${MAX_MEMORY}" ]]; then
    cmd+=" --max-memory $(printf '%q' "${MAX_MEMORY}")"
  fi
  cmd+=" --cpp-std $(printf '%q' "${CPP_STD}")"
  if [[ -n "${CPP_FLAGS}" ]]; then
    cmd+=" --cpp-flags $(printf '%q' "${CPP_FLAGS}")"
  fi
  cmd+=" $(printf '%q' "${remote}")"

  local solve_rc=0
  docker exec "${NAME}" bash -lc "${cmd}" || solve_rc=$?

  rm -rf "${out}"
  local copy_rc=0
  if docker exec "${NAME}" test -d "${remote}/klee-out" 2>/dev/null; then
    info "docker cp ${NAME}:${remote}/klee-out -> ${out}"
    docker cp "${NAME}:${remote}/klee-out" "${out}" || copy_rc=$?
  else
    echo "WARN: container solver did not create ${remote}/klee-out" >&2
  fi
  docker exec "${NAME}" rm -rf "${remote}" || true

  if [[ "${copy_rc}" -ne 0 ]]; then
    echo "WARN: failed to copy solver diagnostics from container (docker cp rc=${copy_rc})" >&2
    if [[ "${solve_rc}" -eq 0 ]]; then
      return "${copy_rc}"
    fi
  fi

  if [[ "${solve_rc}" -ne 0 ]]; then
    echo "ERROR: solver failed with rc=${solve_rc}; copied partial diagnostics to ${out} when available" >&2
    for diag in info messages.txt warnings.txt; do
      if [[ -f "${out}/${diag}" ]]; then
        echo "--- ${diag} (tail) ---" >&2
        tail -25 "${out}/${diag}" >&2 || true
      fi
    done
    return "${solve_rc}"
  fi

  info "求解完成: ${out}"
  [[ -f "${out}/info" ]] && tail -25 "${out}/info" || true

  # 把容器内 ws 路径暴露给上层（solve-and-replay 需要复用同一个 ws）
  echo "${remote}" > /tmp/klee_last_remote_ws.txt 2>/dev/null || true
}

# 内部：替容器内 ws (含 .cpp + klee-out/) 跑 replay，并把 replay/ docker cp 回宿主。
_run_replay_on_remote_ws() {
  local remote="$1" host_replay_out="$2"
  local replay; replay="$(tool_path_in_container replay)"
  [[ -n "${replay}" ]] || die "容器内未发现 replay 脚本"

  # 把 host 上的 replay_list（如指定）拷进容器
  local remote_list=""
  if [[ -n "${REPLAY_LIST}" ]]; then
    [[ -f "${REPLAY_LIST}" ]] || die "--replay-list 指定的文件不存在: ${REPLAY_LIST}"
    remote_list="${remote}/replay_list.txt"
    info "docker cp ${REPLAY_LIST} -> ${NAME}:${remote_list}"
    docker cp "${REPLAY_LIST}" "${NAME}:${remote_list}"
  fi

  info "在容器内执行 replay: ${replay} ${remote}"
  info "    --cpp-std=${CPP_STD}${CPP_FLAGS:+ --cpp-flags=\"${CPP_FLAGS}\"}${REPLAY_PARALLEL:+ --parallel=${REPLAY_PARALLEL}}${remote_list:+ --list=${remote_list}}"

  local cmd
  cmd="$(printf '%q' "${replay}")"
  cmd+=" --cpp-std $(printf '%q' "${CPP_STD}")"
  if [[ -n "${CPP_FLAGS}" ]]; then
    cmd+=" --cpp-flags $(printf '%q' "${CPP_FLAGS}")"
  fi
  if [[ -n "${REPLAY_PARALLEL}" ]]; then
    cmd+=" --parallel $(printf '%q' "${REPLAY_PARALLEL}")"
  fi
  if [[ -n "${remote_list}" ]]; then
    cmd+=" --list $(printf '%q' "${remote_list}")"
  fi
  cmd+=" $(printf '%q' "${remote}")"

  docker exec "${NAME}" bash -lc "${cmd}"

  rm -rf "${host_replay_out}"
  info "docker cp ${NAME}:${remote}/replay -> ${host_replay_out}"
  docker cp "${NAME}:${remote}/replay" "${host_replay_out}"
  if [[ -f "${host_replay_out}/index.json" ]]; then
    info "replay 完成: $(cat "${host_replay_out}/index.json")"
  else
    info "replay 完成: ${host_replay_out}"
  fi
}

cmd_replay() {
  local pos=()
  while [[ $# -gt 0 ]]; do
    local rc=0
    parse_run_arg "$@"    || rc=$?
    if [[ $rc -gt 0 ]]; then shift "$rc"; continue; fi
    parse_global_arg "$@" || rc=$?
    if [[ $rc -gt 0 ]]; then shift "$rc"; continue; fi
    pos+=("$1"); shift
  done

  local src="${pos[0]:-}"
  local klee_out="${pos[1]:-}"
  local host_out="${pos[2]:-}"
  [[ -n "${src}"      ]] || die "用法: $0 replay <src> <klee_out_dir> [host_replay_out] [--cpp-std STD] [--cpp-flags FLAGS] [--replay-parallel N] [--replay-list LIST]"
  [[ -n "${klee_out}" ]] || die "需要 <klee_out_dir>（即 ./klee.sh solve 的输出目录）"
  [[ -d "${klee_out}" ]] || die "不存在的目录: ${klee_out}"

  if [[ -z "${host_out}" ]]; then
    host_out="$(cd "${klee_out}/.." && pwd)/replay"
  fi

  ensure_container_ready
  local ts; ts="$(date +%s)_$$"
  local remote="/tmp/klee_ws_${ts}"
  push_src_to_container "${src}" "${remote}"

  # 把宿主 klee-out 拷成容器内 <remote>/klee-out
  info "docker cp ${klee_out} -> ${NAME}:${remote}/klee-out"
  docker exec "${NAME}" rm -rf "${remote}/klee-out"
  docker cp "${klee_out}" "${NAME}:${remote}/klee-out"

  _run_replay_on_remote_ws "${remote}" "${host_out}"
  docker exec "${NAME}" rm -rf "${remote}" || true
}

cmd_solve_and_replay() {
  local pos=()
  while [[ $# -gt 0 ]]; do
    local rc=0
    parse_run_arg "$@"    || rc=$?
    if [[ $rc -gt 0 ]]; then shift "$rc"; continue; fi
    parse_global_arg "$@" || rc=$?
    if [[ $rc -gt 0 ]]; then shift "$rc"; continue; fi
    pos+=("$1"); shift
  done

  local src="${pos[0]:-}"
  local out_root="${pos[1]:-${ROOT}/klee-out-last}"
  [[ -n "${src}" ]] || die "用法: $0 solve-and-replay <src> <out_root> [--max-seconds N] [--max-forks N] [--only-output-states-covering-new] [--cpp-std STD] [--cpp-flags FLAGS] [--replay-parallel N] [--replay-list LIST]"

  ensure_container_ready
  local solver; solver="$(tool_path_in_container solve)"
  [[ -n "${solver}" ]] || die "容器内未发现 solve 脚本"

  local ts; ts="$(date +%s)_$$"
  local remote="/tmp/klee_ws_${ts}"
  push_src_to_container "${src}" "${remote}"

  info "在容器内执行 solver: ${solver} ${remote}"
  info "    --max-seconds=${MAX_SECONDS} --max-forks=${MAX_FORKS}${ONLY_OUTPUT_STATES_COVERING_NEW:+ --only-output-states-covering-new} --cpp-std=${CPP_STD}${CPP_FLAGS:+ --cpp-flags=\"${CPP_FLAGS}\"}"
  local cmd
  cmd="$(printf '%q' "${solver}")"
  cmd+=" --max-seconds $(printf '%q' "${MAX_SECONDS}")"
  cmd+=" --max-forks $(printf '%q' "${MAX_FORKS}")"
  if [[ -n "${ONLY_OUTPUT_STATES_COVERING_NEW}" ]]; then
    cmd+=" --only-output-states-covering-new"
  fi
  cmd+=" --cpp-std $(printf '%q' "${CPP_STD}")"
  if [[ -n "${CPP_FLAGS}" ]]; then
    cmd+=" --cpp-flags $(printf '%q' "${CPP_FLAGS}")"
  fi
  cmd+=" $(printf '%q' "${remote}")"
  docker exec "${NAME}" bash -lc "${cmd}"

  mkdir -p "${out_root}"
  local host_klee_out="${out_root}/klee-out"
  local host_replay_out="${out_root}/replay"

  rm -rf "${host_klee_out}"
  info "docker cp ${NAME}:${remote}/klee-out -> ${host_klee_out}"
  docker cp "${NAME}:${remote}/klee-out" "${host_klee_out}"

  _run_replay_on_remote_ws "${remote}" "${host_replay_out}"
  docker exec "${NAME}" rm -rf "${remote}" || true

  info "全部完成: ${out_root}"
  [[ -f "${host_klee_out}/info" ]] && tail -10 "${host_klee_out}/info" || true
}

cmd_dump_layout() {
  # ./klee.sh dump-layout <ws_dir> <host_out_json> [--cpp-std STD] [--cpp-flags FLAGS]
  #
  # 在 ``klee-solver-stable`` 容器内编译 ``<ws_dir>/dump_layout.cpp`` 并执行，
  # printf 出 ``layout_core.json``；shell 侧再合并 target.triple / clang_version /
  # compile_flags 形成最终 ``layout_manifest.json`` 落到 ``<host_out_json>``。
  #
  # 容器/镜像复用：与 solve / replay 同一个长生存期容器（``ensure_container_ready``）。
  # 不引入新镜像、新容器；不写入 KLEE_REPLAY 之类的容器内 baked tool。
  #
  # 失败语义：任意一步（编译/执行/cp/JSON 拼接）失败即 die，不静默降级。
  # 详见 LAYOUT_MANIFEST_PROPOSAL.md v2 §4.3。
  local pos=()
  while [[ $# -gt 0 ]]; do
    local rc=0
    parse_run_arg "$@"    || rc=$?
    if [[ $rc -gt 0 ]]; then shift "$rc"; continue; fi
    parse_global_arg "$@" || rc=$?
    if [[ $rc -gt 0 ]]; then shift "$rc"; continue; fi
    pos+=("$1"); shift
  done

  local ws="${pos[0]:-}"
  local out_json="${pos[1]:-}"
  [[ -n "${ws}"       ]] || die "用法: $0 dump-layout <ws_dir> <host_out_json> [--cpp-std STD] [--cpp-flags FLAGS]"
  [[ -n "${out_json}" ]] || die "需要 <host_out_json>（最终 layout_manifest.json 落盘路径）"
  if [[ -f "${ws}" ]]; then
    die "<ws_dir> 必须是目录（含 dump_layout.cpp 等）；当前传入的是文件: ${ws}"
  fi
  [[ -d "${ws}" ]] || die "<ws_dir> 不存在: ${ws}"
  [[ -f "${ws}/dump_layout.cpp" ]] || die "缺少 ${ws}/dump_layout.cpp（应由 KleeWorkspaceBuilder 渲染）"
  [[ -f "${ws}/proto_models.hpp" ]] || die "缺少 ${ws}/proto_models.hpp（dump_layout.cpp 依赖）"
  [[ -f "${ws}/klee_repeated.hpp" ]] || die "缺少 ${ws}/klee_repeated.hpp（dump_layout.cpp 依赖）"

  ensure_container_ready

  local ts; ts="$(date +%s)_$$"
  local remote="/tmp/dump_ws_${ts}"
  push_src_to_container "${ws}" "${remote}"

  info "在容器内编译 dump_layout.cpp (${CPP_STD}${CPP_FLAGS:+ ${CPP_FLAGS}})"
  # 直接在容器内 cd 到 ws，让 ``#include "proto_models.hpp"`` 等 leaf header
  # 走当前目录搜索；与 solve_workspace.sh 的工作模式一致。
  #
  # **clang++ 编译 + g++ 链接** 的两步式：部分镜像里 clang++ 调 ld 时
  # ``posix_spawn`` 会被宿主 seccomp 拦下（compile-only 不会触发，因此
  # solve 阶段不受影响；replay_workspace.sh 也按这个模式回退到 g++）。
  # layout 由 clang 前端的 ``sizeof`` / ``offsetof`` / ``alignof`` 决定，
  # link 阶段不影响 layout 数值——所以这里用 g++ 链接也不破坏 manifest
  # 与 solve compile 同源的语义。target.clang_version 仍记录 clang 版本，
  # 方便 PR-2 decoder 校验。
  local compile_cmd
  compile_cmd="cd $(printf '%q' "${remote}") && clang++ ${CPP_STD}"
  if [[ -n "${CPP_FLAGS}" ]]; then
    compile_cmd+=" ${CPP_FLAGS}"
  fi
  compile_cmd+=" -O0 -g -c dump_layout.cpp -o dump_layout.o"
  if ! docker exec "${NAME}" bash -lc "${compile_cmd}"; then
    docker exec "${NAME}" rm -rf "${remote}" || true
    die "容器内 clang++ 编译 dump_layout.cpp 失败"
  fi

  info "在容器内用 g++ 链接 dump_layout.o → dump_layout"
  local link_cmd
  link_cmd="cd $(printf '%q' "${remote}") && "
  link_cmd+='if command -v g++ >/dev/null 2>&1; then CXX_LINK=g++; '
  link_cmd+='elif command -v clang++ >/dev/null 2>&1; then CXX_LINK=clang++; '
  link_cmd+='else echo "no g++/clang++" >&2; exit 1; fi && '
  link_cmd+='"$CXX_LINK" dump_layout.o -o dump_layout'
  if ! docker exec "${NAME}" bash -lc "${link_cmd}"; then
    docker exec "${NAME}" rm -rf "${remote}" || true
    die "容器内链接 dump_layout 失败"
  fi

  info "在容器内执行 dump_layout，捕获 stdout 为 layout_core.json"
  local run_cmd
  run_cmd="cd $(printf '%q' "${remote}") && ./dump_layout"
  local core_local
  core_local="$(mktemp)"
  if ! docker exec "${NAME}" bash -lc "${run_cmd}" > "${core_local}"; then
    docker exec "${NAME}" rm -rf "${remote}" || true
    rm -f "${core_local}"
    die "容器内执行 dump_layout 失败（layout_core.json 未生成）"
  fi

  # 容器侧采集 target / clang 元数据，shell 侧合并进 layout_manifest.json
  local triple clang_ver
  triple="$(docker exec "${NAME}" bash -lc 'clang++ -dumpmachine 2>/dev/null')" || true
  clang_ver="$(docker exec "${NAME}" bash -lc 'clang++ --version 2>/dev/null | head -1')" || true

  # 合并：用 python 做安全 JSON 合并；避免 sed/awk 在多行 JSON 上踩坑。
  if ! command -v python3 >/dev/null 2>&1; then
    docker exec "${NAME}" rm -rf "${remote}" || true
    rm -f "${core_local}"
    die "本机缺少 python3；klee.sh dump-layout 需要 python3 做 JSON 合并"
  fi

  local out_dir
  out_dir="$(dirname "${out_json}")"
  mkdir -p "${out_dir}" || die "无法创建输出目录 ${out_dir}"

  CORE_JSON="${core_local}" \
  TRIPLE="${triple}" \
  CLANG_VER="${clang_ver}" \
  CPP_STD_VAL="${CPP_STD}" \
  CPP_FLAGS_VAL="${CPP_FLAGS}" \
  OUT_JSON="${out_json}" \
  python3 - <<'PY'
import json, os, sys
core_path = os.environ["CORE_JSON"]
out_path  = os.environ["OUT_JSON"]
triple    = (os.environ.get("TRIPLE") or "").strip()
clang_ver = (os.environ.get("CLANG_VER") or "").strip()
cpp_std   = (os.environ.get("CPP_STD_VAL") or "").strip()
cpp_flags = (os.environ.get("CPP_FLAGS_VAL") or "").strip()
with open(core_path, "r", encoding="utf-8") as f:
    data = json.load(f)
target = data.setdefault("target", {})
if triple:
    target["triple"] = triple
if clang_ver:
    target["clang_version"] = clang_ver
flags = []
if cpp_std:
    flags.append(cpp_std)
if cpp_flags:
    flags.extend(cpp_flags.split())
flags.extend(["-O0", "-g"])
target["compile_flags"] = flags
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)
    f.write("\n")
PY
  rc=$?
  rm -f "${core_local}"
  docker exec "${NAME}" rm -rf "${remote}" || true
  if [[ $rc -ne 0 ]]; then
    die "合并 layout_core.json + target 元数据失败 (python3 rc=${rc})"
  fi

  info "dump-layout 完成: ${out_json}"
}

cmd_selftest() {
  [[ -f "${EXAMPLE_CPP}" ]] || die "缺少示例 ${EXAMPLE_CPP}"
  local out="${ROOT}/klee-out-selftest"
  MAX_SECONDS="15" MAX_FORKS="5000" \
    cmd_solve "${EXAMPLE_CPP}" "${out}"

  test -f "${out}/info" || die "selftest: 缺少 ${out}/info"
  grep -q "completed paths = 3" "${out}/info" \
    || die "selftest: 期望 completed paths = 3，请查看 ${out}/info"
  info "selftest PASS（${out}/info 完成路径数 = 3）"
}

# -------- 入口 ---------------------------------------------------------------

case "${1:-}" in
  prepare)          shift; cmd_prepare "$@" ;;
  start)            shift; cmd_start "$@" ;;
  stop)             shift; cmd_stop "$@" ;;
  rm)               shift; cmd_rm "$@" ;;
  status)           shift; cmd_status "$@" ;;
  shell)            shift; cmd_shell "$@" ;;
  build-image)      shift; cmd_build_image "$@" ;;
  solve)            shift; cmd_solve "$@" ;;
  replay)           shift; cmd_replay "$@" ;;
  solve-and-replay) shift; cmd_solve_and_replay "$@" ;;
  dump-layout)      shift; cmd_dump_layout "$@" ;;
  selftest)         shift; cmd_selftest "$@" ;;
  ""|-h|--help|help)
    sed -n '2,65p' "$0"
    ;;
  *)
    echo "未知子命令: $1" >&2
    echo "可用: prepare | start | stop | rm | status | shell | build-image | solve | replay | solve-and-replay | dump-layout | selftest" >&2
    exit 1
    ;;
esac
